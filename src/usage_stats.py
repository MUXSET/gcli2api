"""
Usage statistics module for tracking API calls per credential file.
Simplified version: only tracks 24h successful call counts.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Dict, Optional

from config import get_credentials_dir, is_mongodb_mode
from log import log

from .state_manager import get_state_manager
from .storage_adapter import get_storage_adapter


def _get_24h_ago() -> datetime:
    """Get the timestamp for 24 hours ago."""
    return datetime.now(timezone.utc) - timedelta(hours=24)


class UsageStats:
    """
    Simplified usage statistics manager.
    Only tracks successful API calls in the last 24 hours.
    """

    def __init__(self):
        self._lock = Lock()
        self._state_file = None
        self._state_manager = None
        self._storage_adapter = None
        self._stats_cache: Dict[str, Dict[str, Any]] = {}
        self._initialized = False
        self._cache_dirty = False
        self._last_save_time = 0
        self._save_interval = 60  # Save at most once per minute
        self._max_cache_size = 100

    async def initialize(self):
        """Initialize the usage stats module."""
        if self._initialized:
            return

        self._storage_adapter = await get_storage_adapter()

        if not await is_mongodb_mode():
            credentials_dir = await get_credentials_dir()
            self._state_file = os.path.join(credentials_dir, "creds_state.toml")
            self._state_manager = get_state_manager(self._state_file)

        await self._load_stats()
        self._initialized = True
        storage_type = "MongoDB" if await is_mongodb_mode() else "File"
        log.debug(f"Usage statistics module initialized with {storage_type} storage backend")

    def _normalize_filename(self, filename: str) -> str:
        """Normalize filename to relative path for consistent storage."""
        if not filename:
            return ""

        # Normalize separators to forward slash then take basename
        # This handles mixed separators (e.g. on Windows) correctly
        normalized = filename.replace("\\", "/")
        if "/" in normalized:
            return normalized.split("/")[-1]
            
        return filename

    async def _load_stats(self):
        """Load statistics from unified storage"""
        try:
            import asyncio

            async def load_stats_with_timeout():
                all_usage_stats = await self._storage_adapter.get_all_usage_stats()

                stats_cache = {}
                processed_count = 0

                for filename, stats_data in all_usage_stats.items():
                    if isinstance(stats_data, dict):
                        normalized_filename = self._normalize_filename(filename)

                        # Only load call_timestamps
                        usage_data = {
                            "call_timestamps": stats_data.get("call_timestamps", []),
                        }

                        # Only cache if there are actual timestamps
                        if usage_data.get("call_timestamps"):
                            stats_cache[normalized_filename] = usage_data
                            processed_count += 1

                return stats_cache, processed_count

            try:
                self._stats_cache, processed_count = await asyncio.wait_for(
                    load_stats_with_timeout(), timeout=15.0
                )
                log.debug(f"Loaded usage statistics for {processed_count} credential files")
            except asyncio.TimeoutError:
                log.error("Loading usage statistics timed out after 15 seconds, using empty cache")
                self._stats_cache = {}
                return

        except Exception as e:
            log.error(f"Failed to load usage statistics: {e}")
            self._stats_cache = {}

    async def _save_stats(self):
        """Save statistics to unified storage."""
        current_time = time.time()

        if not self._cache_dirty or (current_time - self._last_save_time < self._save_interval):
            return

        try:
            saved_count = 0
            for filename, stats in self._stats_cache.items():
                try:
                    stats_data = {
                        "call_timestamps": stats.get("call_timestamps", []),
                    }

                    success = await self._storage_adapter.update_usage_stats(filename, stats_data)
                    if success:
                        saved_count += 1
                except Exception as e:
                    log.error(f"Failed to save stats for {filename}: {e}")
                    continue

            self._cache_dirty = False
            self._last_save_time = current_time
            log.debug(
                f"Successfully saved {saved_count}/{len(self._stats_cache)} usage statistics to unified storage"
            )
        except Exception as e:
            log.error(f"Failed to save usage statistics: {e}")

    def _get_or_create_stats(self, filename: str) -> Dict[str, Any]:
        """Get or create statistics entry for a credential file."""
        normalized_filename = self._normalize_filename(filename)

        if normalized_filename not in self._stats_cache:
            # Control cache size - remove oldest entry if limit reached
            if len(self._stats_cache) >= self._max_cache_size:
                # Remove entry with oldest/fewest timestamps
                oldest_key = min(
                    self._stats_cache.keys(),
                    key=lambda k: len(self._stats_cache[k].get("call_timestamps", [])),
                )
                del self._stats_cache[oldest_key]
                self._cache_dirty = True
                log.debug(f"Removed oldest usage stats cache entry: {oldest_key}")

            self._stats_cache[normalized_filename] = {
                "call_timestamps": [],
                "latency_history": [],
            }
            self._cache_dirty = True

        return self._stats_cache[normalized_filename]

    def _cleanup_old_timestamps(self, stats: Dict[str, Any]):
        """Remove timestamps and latency records older than 24 hours."""
        cutoff_time = _get_24h_ago()
        
        # 1. Cleanup Timestamps
        timestamps = stats.get("call_timestamps", [])
        if timestamps:
            new_timestamps = []
            for ts in timestamps:
                try:
                    dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc) if datetime.fromisoformat(ts).tzinfo is None else datetime.fromisoformat(ts)
                    if dt > cutoff_time:
                        new_timestamps.append(ts)
                except Exception:
                    continue
            if len(new_timestamps) != len(timestamps):
                stats["call_timestamps"] = new_timestamps
                self._cache_dirty = True

        # 2. Cleanup Latency History
        latency_history = stats.get("latency_history", [])
        if latency_history:
            new_history = []
            for record in latency_history:
                try:
                    # record is [timestamp, latency_ms]
                    ts = record[0]
                    dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc) if datetime.fromisoformat(ts).tzinfo is None else datetime.fromisoformat(ts)
                    if dt > cutoff_time:
                        new_history.append(record)
                except Exception:
                    continue
            if len(new_history) != len(latency_history):
                stats["latency_history"] = new_history
                self._cache_dirty = True

    async def record_successful_call(self, filename: str, latency_ms: float = 0.0, model_name: str = None, user_id: str = None):
        """Record a successful API call with latency."""
        if not self._initialized:
            await self.initialize()

        with self._lock:
            try:
                current_time = datetime.now(timezone.utc).isoformat()
                
                # Helper to update stats dict
                def update_stats(target_stats):
                    self._cleanup_old_timestamps(target_stats)
                    target_stats["call_timestamps"].append(current_time)
                    
                    if "latency_history" not in target_stats:
                        target_stats["latency_history"] = []
                    target_stats["latency_history"].append([current_time, latency_ms])

                # 1. Record against the credential file
                normalized_filename = self._normalize_filename(filename)
                
                stats = self._get_or_create_stats(normalized_filename)
                update_stats(stats)
                
                # 2. Record against the user (if provided)
                if user_id:
                    user_key = f"USER_stats_{user_id}"
                    user_stats = self._get_or_create_stats(user_key)
                    update_stats(user_stats)

                self._cache_dirty = True
                
                call_count = len(stats["call_timestamps"])
                log.debug(f"Usage recorded - File: {normalized_filename}: {call_count}, Latency: {latency_ms}ms, User: {user_id}")

            except Exception as e:
                log.error(f"Failed to record usage statistics: {e}")

        # Save stats asynchronously
        try:
            await self._save_stats()
        except Exception as e:
            log.error(f"Failed to save usage statistics after recording: {e}")
            
    async def get_hourly_latency_trends(self) -> Dict[str, Any]:
        """Aggregate latency stats by hour for the last 24 hours."""
        if not self._initialized:
            await self.initialize()
            
        trends = {
            "labels": [],
            "avg_latency": [],
            "p95_latency": []
        }
        
        # Initialize hourly buckets
        now = datetime.now(timezone.utc)
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        start_time = current_hour - timedelta(hours=23)
        
        hourly_data = {} # { hour_label: [latencies...] }
        
        # Generate labels
        for i in range(24):
            hour_time = start_time + timedelta(hours=i)
            label = hour_time.strftime("%H:00")
            trends["labels"].append(label)
            hourly_data[label] = []
            
        # Collect all latency records from all credentials (excluding USER_stats)
        with self._lock:
            for filename, data in self._stats_cache.items():
                if filename.startswith("USER_stats_") or filename.startswith("_"):
                    continue
                    
                history = data.get("latency_history", [])
                for record in history:
                    try:
                        ts, latency = record
                        dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc) if datetime.fromisoformat(ts).tzinfo is None else datetime.fromisoformat(ts)
                        
                        if dt < start_time:
                            continue
                            
                        # Find bucket (naive approach)
                        # Round down to hour
                        bucket_dt = dt.replace(minute=0, second=0, microsecond=0)
                        label = bucket_dt.strftime("%H:00")
                        
                        if label in hourly_data:
                            hourly_data[label].append(latency)
                    except Exception:
                        continue
                        
        # Calculate stats
        for label in trends["labels"]:
            latencies = hourly_data[label]
            if not latencies:
                trends["avg_latency"].append(0)
                trends["p95_latency"].append(0)
            else:
                avg = sum(latencies) / len(latencies)
                trends["avg_latency"].append(round(avg, 2))
                
                # P95
                latencies.sort()
                p95_index = int(len(latencies) * 0.95)
                trends["p95_latency"].append(round(latencies[p95_index], 2))
                
        return trends
            
    async def get_usage_stats(self, filename: str = None) -> Dict[str, Any]:
        """Get usage statistics."""
        if not self._initialized:
            await self.initialize()

        with self._lock:
            if filename:
                normalized_filename = self._normalize_filename(filename)
                stats = self._get_or_create_stats(normalized_filename)
                self._cleanup_old_timestamps(stats)

                return {
                    "calls_24h": len(stats.get("call_timestamps", [])),
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
            
            else:
                all_stats = {}
                for filename, stats in self._stats_cache.items():
                    self._cleanup_old_timestamps(stats)
                    all_stats[filename] = {
                        "calls_24h": len(stats.get("call_timestamps", [])),
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    }
                return all_stats

    async def get_hourly_usage_trends(self) -> Dict[str, Any]:
        """Aggregate usage stats into hourly buckets for the last 24 hours."""
        if not self._initialized:
            await self.initialize()

        with self._lock:
            # Initialize empty buckets for last 24h
            now = datetime.now(timezone.utc)
            # Round down to nearest hour
            current_hour = now.replace(minute=0, second=0, microsecond=0)
            
            # Generate labels (e.g. "14:00") and initialize counts
            labels = []
            counts = [0] * 24
            
            # We want -23h to Now (0h)
            for i in range(23, -1, -1):
                t = current_hour - timedelta(hours=i)
                labels.append(t.strftime("%H:00"))

            cutoff_time = _get_24h_ago()

            # Iterate all cached stats
            for filename, stats in self._stats_cache.items():
                # Skip User aggregates to avoid double counting if we are summing file stats
                # or decided to show total system load. 
                # "USER_stats_" keys are aggregates of user calls. 
                # Credential files are the source.
                # If we sum credential files, we get total system load.
                if filename.startswith("USER_stats_"):
                    continue
                    
                timestamps = stats.get("call_timestamps", [])
                for ts in timestamps:
                    try:
                        dt = datetime.fromisoformat(ts)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        
                        if dt > cutoff_time:
                            # Calculate hour difference from now
                            # But easier to just map to 0-23 index
                            # Index 23 is current hour. Index 0 is 23 hours ago.
                            
                            # Identify which bucket implies:
                            # bucket_index = 23 - (hours_ago)
                            
                            diff = now - dt
                            hours_ago = int(diff.total_seconds() / 3600)
                            
                            if 0 <= hours_ago < 24:
                                index = 23 - hours_ago
                                if 0 <= index < 24:
                                    counts[index] += 1
                    except:
                        continue
            
            return {
                "labels": labels,
                "data": counts,
                "total_24h": sum(counts)
            }

    async def get_aggregated_stats(self) -> Dict[str, Any]:
        """Get aggregated statistics across all credential files."""
        if not self._initialized:
            await self.initialize()

        all_stats = await self.get_usage_stats()

        total_calls = 0
        total_files = len(all_stats)

        for stats in all_stats.values():
            total_calls += stats["calls_24h"]

        return {
            "total_files": total_files,
            "total_calls_24h": total_calls,
            "avg_calls_per_file": total_calls / max(total_files, 1),
        }

    async def reset_stats(self, filename: str = None):
        """Reset usage statistics."""
        if not self._initialized:
            await self.initialize()

        with self._lock:
            if filename:
                normalized_filename = self._normalize_filename(filename)
                if normalized_filename in self._stats_cache:
                    self._stats_cache[normalized_filename]["call_timestamps"] = []
                    self._cache_dirty = True
                    log.info(f"Reset usage statistics for {normalized_filename}")
            else:
                # Reset all statistics
                for stats in self._stats_cache.values():
                    stats["call_timestamps"] = []
                self._cache_dirty = True
                log.info("Reset usage statistics for all credential files")

        await self._save_stats()


# Global instance
_usage_stats_instance: Optional[UsageStats] = None


async def get_usage_stats_instance() -> UsageStats:
    """Get the global usage statistics instance."""
    global _usage_stats_instance
    if _usage_stats_instance is None:
        _usage_stats_instance = UsageStats()
        await _usage_stats_instance.initialize()
    return _usage_stats_instance


async def record_successful_call(filename: str, latency_ms: float = 0.0, model_name: str = None, user_id: str = None):
    """Convenience function to record a successful API call."""
    stats = await get_usage_stats_instance()
    await stats.record_successful_call(filename, latency_ms, model_name, user_id)


async def get_usage_stats(filename: str = None) -> Dict[str, Any]:
    """Convenience function to get usage statistics."""
    stats = await get_usage_stats_instance()
    return await stats.get_usage_stats(filename)


async def get_aggregated_stats() -> Dict[str, Any]:
    """Convenience function to get aggregated statistics."""
    stats = await get_usage_stats_instance()
    return await stats.get_aggregated_stats()

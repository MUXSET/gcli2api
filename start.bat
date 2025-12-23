@rem git fetch --all
@rem for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set branch=%%b
@rem git reset --hard origin/%branch%
uv sync
call .venv\Scripts\activate.bat
python web.py
pause
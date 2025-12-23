# 如何申请 Google OAuth 客户端 ID 和密钥

如果您希望使用自己的 Google Cloud 项目进行认证（更稳定、独立配额），请按照以下步骤申请：

1.  **打开 Google Cloud Console**
    访问 [https://console.cloud.google.com/](https://console.cloud.google.com/) 并登录您的 Google 账号。

2.  **创建或选择项目**
    *   点击左上角的项目选择器。
    *   点击 **"新建项目" (New Project)**，输入项目名称（如 `gcli2api`），然后点击创建。
    *   或者直接选择一个现有的项目。

3.  **配置 OAuth 同意屏幕**
    *   在左侧菜单中，点击 **"API 和服务" (APIs & Services)** > **"OAuth 同意屏幕" (OAuth consent screen)**。
    *   用户类型选择 **"外部" (External)**，点击创建。
    *   **必填项**：只需填写 "应用名称"（如 `gCli Proxy`）、"用户支持电子邮件" 和 "开发者联系信息"。
    *   其他选项（范围、测试用户等）可以直接点击 "保存并继续" 跳过。
    *   **发布应用**：在摘要页面，点击 **"发布应用" (Publish App)** 按钮，确认发布（从测试版转为正式版，避免每7天授权过期）。

4.  **创建凭证 (Client ID)**
    *   点击左侧菜单的 **"凭证" (Credentials)**。
    *   点击页面顶部的 **"+ 创建凭证" (+ Create Credentials)** > **"OAuth 客户端 ID" (OAuth client ID)**。
    *   **应用类型**：选择 **"桌面应用" (Desktop app)**。
    *   **名称**：输入一个名称（如 `Desktop Client`）。
    *   点击 **"创建" (Create)**。

5.  **获取 ID 和密钥**
    *   创建成功后，弹窗会显示您的 **Client ID** 和 **Client Secret**。
    *   请复制这两个值，稍后填入配置文件或环境变量中。

6.  **配置到 gcli2api**
    您可以通过以下两种方式之一配置：

    *   **方式 A：环境变量（推荐）**
        *   `GOOGLE_OAUTH_CLIENT_ID=您的Client_ID`
        *   `GOOGLE_OAUTH_CLIENT_SECRET=您的Client_Secret`

    *   **方式 B：配置文件 (config.toml)**
        ```toml
        google_oauth_client_id = "您的Client_ID"
        google_oauth_client_secret = "您的Client_Secret"
        ```

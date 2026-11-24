"""Connection address shared by Telegram onboarding and the MCP route."""

PUBLIC_HOST = "lbot.zapleo.com"
MOUNT = "/mcp"
MCP_PATH = "/catalog"
MCP_URL = f"https://{PUBLIC_HOST}{MOUNT}{MCP_PATH}"
PERSONAL_PATH = "/personal"
PERSONAL_URL = f"https://{PUBLIC_HOST}{MOUNT}{PERSONAL_PATH}"

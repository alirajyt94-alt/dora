RAJ MUSIC PRO — Railway setup

FILES
- bot.py: fixed bot source
- requirements.txt: Python dependencies
- nixpacks.toml: installs Python, FFmpeg and libsodium
- Procfile: worker startup command
- .env.example: environment variable template (copy values into Railway Variables)

RAILWAY STEPS
1. Upload/push bot.py, requirements.txt, nixpacks.toml and Procfile to the same GitHub repository.
2. In Railway, create a project from that repository.
3. Open the service's Variables tab and add:
   DISCORD_TOKEN = your Discord bot token
   GUILD_ID = your server ID (optional, recommended for quick slash-command sync)
   AUTO_DISCONNECT_MINUTES = 5
   OWNER_IDS = (optional)
   YOUTUBE_COOKIES_FILE = (leave blank unless a cookies file is securely available at a real path)
4. Redeploy and inspect Deploy Logs. The bot should log that it is online.
5. In Discord Developer Portal, enable the bot and invite it with bot + applications.commands scopes and permissions to View Channels, Send Messages, Embed Links, Connect and Speak.
6. If using prefix commands in addition to slash commands, enable Message Content Intent in the Developer Portal. This bot's main controls are slash commands.
7. In Discord, run /healthcheck. If YouTube returns “Sign in to confirm you're not a bot”, that is an upstream YouTube/Railway restriction, not a Python syntax error. A valid cookies file may help, but is not guaranteed. Do not put cookies or your bot token in a public repository.

NOTES
- The source passed Python syntax/AST validation here. A real Discord login, voice connection, FFmpeg stream, and YouTube extraction cannot be fully tested without your Railway environment and credentials.
- If a song fails, run /healthcheck and inspect Railway logs for the underlying yt-dlp error.

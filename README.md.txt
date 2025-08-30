# Telegram SoundCloud Bot (Python)

Бот для завантаження дозволених треків із SoundCloud у Telegram.  
Працює на Python + [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) + [yt-dlp](https://github.com/yt-dlp/yt-dlp).

---

## 📦 Встановлення (на сервері)

```bash
# SSH у VM
ssh -i ~/.ssh/YOUR_KEY.pem ubuntu@YOUR_PUBLIC_IP

# Клонування репозиторію
sudo mkdir -p /opt/sc_telegram_bot
sudo chown -R ubuntu:ubuntu /opt/sc_telegram_bot
cd /opt/sc_telegram_bot
git clone <REPO_URL> .

# Python + залежності
sudo apt update
sudo apt install -y python3-venv python3-pip ffmpeg git

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
deactivate

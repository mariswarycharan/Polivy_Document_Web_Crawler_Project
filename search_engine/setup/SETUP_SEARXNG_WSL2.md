# Setting up SearXNG on Windows with WSL2 (no Docker)

SearXNG runs only on Linux. These steps put a minimal Debian on the **E: drive** (C: is short on space)
and install SearXNG inside it. Rough disk use: 0.5-1.5 GB. Nothing runs in the background unless you start it.

## 1. Install WSL (once) - PowerShell **as Administrator**

```powershell
wsl --install --no-distribution
```
Restart Windows if asked. If WSL is already installed, run `wsl --update` instead.

## 2. Install Debian on the E: drive - PowerShell (normal)

```powershell
mkdir E:\WSL
wsl --install --distribution Debian --location E:\WSL\Debian
```
It asks for a Linux user name and password - choose any, and remember the password (it is used for `sudo`).

If `--location` is not recognised, your WSL is older: run `wsl --update`, then try again.

## 3. Install SearXNG inside Debian

Open Debian (`wsl -d Debian`) and run:

```bash
sudo apt-get update
sudo apt-get install -y python3-dev python3-babel python3-venv git build-essential \
    libxslt-dev zlib1g-dev libffi-dev libssl-dev openssl

git clone --depth 1 https://github.com/searxng/searxng ~/searxng-src
python3 -m venv ~/searx-pyenv
source ~/searx-pyenv/bin/activate
pip install -U pip setuptools wheel pyyaml msgspec typing-extensions pybind11
cd ~/searxng-src && pip install --use-pep517 --no-build-isolation -e .

mkdir -p ~/searxng-settings
cp /mnt/e/Roche/Polivy_Document_Web_Crawler_Project/search_engine/setup/searxng/settings.yml ~/searxng-settings/
sed -i "s/CHANGE_ME_TO_A_RANDOM_KEY/$(openssl rand -hex 32)/" ~/searxng-settings/settings.yml
cp /mnt/e/Roche/Polivy_Document_Web_Crawler_Project/search_engine/setup/start_searxng.sh ~/
sed -i 's/\r$//' ~/start_searxng.sh && chmod +x ~/start_searxng.sh
```

## 4. Start SearXNG (each time you need it)

```bash
wsl -d Debian -- ~/start_searxng.sh
```
Leave that window open. Check it in a browser: <http://127.0.0.1:8888>.

## 5. Check from the project (Windows, a second terminal)

```powershell
cd E:\Roche\Polivy_Document_Web_Crawler_Project
python -m pip install -r search_engine\requirements.txt
python -m search_engine check
```

## Stopping and freeing memory

Ctrl+C in the SearXNG window stops it. `wsl --shutdown` (PowerShell) stops WSL completely.

# NEMESIS - Neural Voice Interface

A free, local, Jarvis-style voice assistant for Windows with an Android phone as the microphone.
It listens, thinks with a local language model (Ollama), answers in a deep robotic voice, and shows
a futuristic HUD in the browser. No paid services. Built to run on an old PC with no GPU.

## What it can do
- Talk in English, Hindi and Marathi
- Open and close apps, tabs and websites by voice; play YouTube songs
- Camera vision on the phone: read text (OCR), identify colours and objects
- Face detection and recognising only the people you enrol (data stays on your PC, encrypted)
- Weather, news, maths, reminders, notes and tasks
- Offline mode for hearing, speech and core commands
- Wake word with a stay-awake window ("Nemesis, ..."), hands-free on the phone
- Optional hardware: switch a light by voice with an Arduino Uno and a relay (`nemesis_light.ino`)
- HUD with a hide-panel button and a clean view for screen recordings

## Design principles
- **Local first.** Speech recognition and the language model run on your own machine.
- **Safety by design.** A permission gate rates every action (read, reversible, sensitive, admin) and asks for
  confirmation or a PIN before anything sensitive. A voice "freeze" stops speech, camera and pending actions at once.
- **Reliable updates.** `python nemesis.py --selftest` runs a command test before every update, and the updater
  rolls back by itself if a test fails. Core files are sealed and checked at start-up.
- **Honest limits.** Face matching is a convenience, not a lock. Do not use it to protect anything important.

## Requirements
- Windows 10 or newer, Python 3.13
- [Ollama](https://ollama.com) with a small model, for example `ollama pull llama3.2:3b`
- An Android phone with Chrome on the same Wi-Fi (or USB tethering) as the PC
- Optional: Tesseract OCR for reading text from the camera

## Install
```
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
python nemesis.py --selftest
python nemesis.py
```
Open the phone link printed in the terminal in Chrome on your phone, accept the one-time certificate warning
(Nemesis makes its own certificate), allow the microphone, and tap the mic button.
Open the HUD at `http://127.0.0.1:8765` on the PC.

Keys: `F` fullscreen, `T` theme colour, `B` background, `P` phone QR, `H` hide or show the side panel.
To use your own HUD background, save a 1920x1080 picture as `hud_bg.jpg` next to `nemesis.py`.

## Light control (optional hardware)
Upload `nemesis_light.ino` to an Arduino Uno. Wire a 5 V relay module: VCC to 5V, GND to GND, IN to pin 7.
Switch only the **12 V side** of an LED strip through the relay's COM and NO terminals. Never open or modify mains
wiring, and unplug power before changing any wire. Run `python light_test.py` to test the relay.

## Tests
`python nemesis.py --selftest` runs the command tests with every side effect switched off.

## Status
Personal project, built step by step on low-end hardware. Feedback and ideas are welcome.

## Licence
MIT. See `LICENSE`.

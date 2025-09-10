# 🧼 Data Scrubber - Secure Wipe Utility
A secure, cross-platform utility (Windows & Linux) to permanently erase data from your drives, making it completely unrecoverable. It uses a friendly Node.js menu and a powerful Python engine.

## ⚠️ CRITICAL WARNING ⚠️
This tool permanently destroys data. Once you confirm a wipe, it's gone forever. Use with extreme caution. We are not responsible for any data loss.

## ✨ Features
🖥️ Interactive Menu: Easy-to-use, no complex commands needed.

💻 Cross-Platform: Works on Windows, Linux.

💿 Drive Scanning: Automatically finds your drives to prevent mistakes.

💥 Multiple Wipe Modes: Choose from Quick, Secure, or Paranoid level wiping.

⏱️ Live Progress & ETA: See a real-time progress bar and get an ETA for the wipe.

🚫 Graceful Cancellation: Safely stop the process anytime with ```Ctrl+C```.

🛡️ No Admin Needed: Runs without administrator/root privileges for safety.

## ⚙️ Requirements
Node.js (v14+)

Python (v3.6+)

## 🚀 Getting Started
Clone the code:

```git clone https://github.com/HeetPatel8126/DataScruber.git```<br>

```cd data-scrubber```

Install dependencies:

npm install

## ▶️ How to Run
On Windows: Double-click start.bat or run ```.\start.bat``` in your terminal.

On Linux: First, make the script executable ```chmod +x start.sh```, then run ```./start.sh```.

## 💥 Wipe Modes Explained
🐇 Quick Wipe: Fast but insecure. Deletes files, but they can be recovered.<br>

🛡️ Secure Wipe (Recommended): Overwrites all data once. Defeats all software recovery tools.<br>

💀 Paranoid Wipe (Very Slow): Overwrites files three times. For maximum security.
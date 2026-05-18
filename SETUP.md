# Manual Windows Setup

Complete the following one-time Windows setup before first use of the CCTV application.

1. **Disable USB selective suspend** — prevents Windows from powering down the camera's USB port when the monitor sleeps:
   Control Panel → Power Options → Change plan settings → Change advanced power settings → USB settings → USB selective suspend setting → **Disabled**

2. **Monitor sleep** — set "Turn off the display" to desired timeout (e.g., 5 minutes). The application does not interfere with this.

3. **System sleep** — suppressed programmatically; no manual setting required.

4. **Screen saver** — set to **None**.

5. **Verify camera driver** — Device Manager → Cameras: camera must appear with no warning icons.

6. **(Optional) Auto-start at logon** — if you want the bot to come up automatically after a reboot so you can toggle the camera on/off remotely without needing to log in physically and double-click the .bat:

   - Press `Win+R`, type `shell:startup`, press Enter. This opens the user's Startup folder (`%AppData%\Microsoft\Windows\Start Menu\Programs\Startup`).
   - Right-click `start_cctv.bat` (in the project directory) → **Create shortcut**.
   - Move the shortcut into the Startup folder.
   - Right-click the shortcut → **Properties** → **Run: Minimized** so the console window doesn't steal focus on each logon.
   - Reboot and confirm: after you log in, a minimized terminal appears and the bot sends "📷 Camera is now ON" within ~5 seconds.

   The startup folder only fires after a user logs in. For truly headless operation (start before any login), use Task Scheduler with "At startup" + "Run whether user is logged on or not" — but that requires storing the user password and configuring console-less Python (e.g., `pythonw`), which is out of scope for this single-user setup. The camera toggle in Telegram lets you keep the app running 24/7 anyway, so the logon-trigger variant is usually enough.

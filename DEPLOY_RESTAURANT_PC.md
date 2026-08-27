# MarginMise — Restaurant PC Deployment Guide

## Requirements
- Windows 10/11
- Python 3.11+ (installed automatically on first run)
- 4GB RAM minimum, 8GB recommended
- 2GB free disk space

## Quick Deployment (Recommended)

1. Copy the entire `MarginMise` folder to the restaurant PC
2. Double-click `run_marginmise.bat`
3. Wait for setup to complete (5-10 minutes on first run)
4. The app will launch automatically

## What Happens on First Run

1. **Python Check**: If Python is not installed, it installs silently via winget
2. **Virtual Environment**: Creates `.venv` folder (isolated Python environment)
3. **Dependencies**: Installs all required packages from `requirements.txt`
4. **Tesseract OCR**: Installs silently for document scanning
5. **AI Model**: Downloads local AI model (~250MB)
6. **Launch**: Opens the MarginMise GUI

## Subsequent Runs

After the first run, just double-click `run_marginmise.bat` again. The app launches in seconds.

## File Structure on Restaurant PC

```
MarginMise/
├── run_marginmise.bat      ← Double-click this to launch
├── requirements.txt        ← Python dependencies
├── launch_gui.py          ← Main application
├── local_ocr.py           ← OCR engine
├── local_ai.py            ← AI assistant
├── .venv/                 ← Python environment (created on first run)
├── Logs/                  ← Application logs
└── [all other Python files]
```

## Troubleshooting

### "Python not found" error
The launcher will attempt to install Python automatically. If it fails:
1. Go to https://python.org/downloads/
2. Download Python 3.12
3. Run installer, check "Add Python to PATH"
4. Re-run `run_marginmise.bat`

### "Not enough memory" error
- Close other applications
- Ensure at least 4GB RAM is available
- Restart the PC if needed

### Tesseract installation fails
The app will still work with RapidOCR. To retry Tesseract:
```cmd
.venv\Scripts\python.exe local_ocr.py ensure --install-tesseract
```

### AI model download fails
The app will use deterministic SQL answers instead. To retry:
```cmd
.venv\Scripts\python.exe local_ai.py ensure
```

## Network Requirements

- **First run**: Internet required for Python, dependencies, Tesseract, and AI model
- **After setup**: No internet required. Everything runs locally.

## Backup and Restore

To backup a restaurant's data:
1. Copy the entire `MarginMise` folder
2. The database is in `.venv/Lib/site-packages/` or check `local_app_data/MarginMise/`

To restore:
1. Copy the backup folder to the new PC
2. Run `run_marginmise.bat`
3. The app will detect existing setup and launch immediately

## Updating

To update to a new version:
1. Replace all files in the `MarginMise` folder with the new version
2. Keep the `.venv` folder (it contains your data and setup)
3. Run `run_marginmise.bat`

## Performance Tips for Restaurant PCs

1. **Close other apps** before launching MarginMise
2. **Disable screen saver** during initial setup (large downloads)
3. **Use wired network** if available for faster first-run setup
4. **Don't move the folder** after setup (paths are cached)

## Support

If deployment fails:
1. Check `Logs/bootstrap.log` for errors
2. Check `Logs/startup_error.log` for GUI errors
3. Contact support with the log files

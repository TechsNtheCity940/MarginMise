# MarginMise — Restaurant PC Deployment Guide

## Requirements
- Windows 10/11
- 4GB RAM minimum, 8GB recommended
- 3GB free disk space for the application, model, and temporary files

## Quick Deployment (Recommended)

1. Install the released Windows package.
2. Launch MarginMise from its desktop or Start Menu shortcut.
3. The bundled GUI starts directly; it does not install Python or create a runtime venv.
4. Install CostPilot from Settings only when needed. Its model is approximately 697 MiB.

## What Happens on First Run

1. **Launch**: Opens the bundled MarginMise GUI.
2. **On demand**: OCR and CostPilot helper processes start only when those features are used.
3. **Optional model setup**: CostPilot downloads its verified runtime/model only after an authorized manager starts installation.

## Subsequent Runs

After installation, use the desktop or Start Menu shortcut. The app launches without a dependency-installation phase.

## File Structure on Restaurant PC

```
%LOCALAPPDATA%\MarginMise\
├── MarginMise.exe          ← Bundled GUI executable
├── Logs\                   ← Startup/runtime logs
└── AI\                     ← Optional verified CostPilot runtime/model

Restaurant workspaces are selected by the user and contain their own
`restaurant_costs.sqlite3` database and operational folders.
```

## Troubleshooting

### "Python not found" error
1. Re-run the released installer or use its repair option.
2. Do not install Python manually for the packaged application.

### "Not enough memory" error
- Close other applications
- Ensure at least 4GB RAM is available
- Restart the PC if needed

### Tesseract installation fails
The app will still work with RapidOCR. To retry Tesseract:
```cmd
OCR is provisioned by the packaged application when invoice processing requires it.
```

### AI model download fails
The app will use deterministic SQL answers instead. To retry:
```cmd
Use Settings → CostPilot → Install or repair local CostPilot.
```

## Network Requirements

- **First run**: Internet required for Python, dependencies, Tesseract, and AI model
- **After setup**: No internet required. Everything runs locally.

## Backup and Restore

To backup a restaurant's data:
1. Back up each selected restaurant workspace, including its `restaurant_costs.sqlite3` file.
2. Optional CostPilot data is stored under `%LOCALAPPDATA%\MarginMise\AI\`.

To restore:
1. Install the released Windows package on the new PC.
2. Launch MarginMise and select the restored restaurant workspace.

## Updating

To update to a new version:
1. Close MarginMise.
2. Run the new Windows installer.
3. Keep the selected restaurant workspace and optional `%LOCALAPPDATA%\MarginMise\AI\` data.

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

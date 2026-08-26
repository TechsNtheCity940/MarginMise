# MarginMise — Restaurant Cost Management

**Talk Trash To Us...Seriously....We love it!**

MarginMise (v3.5.0) is an all-in-one restaurant cost management system that runs entirely on your local machine — no cloud, no monthly fees, no internet required after install. It handles invoice processing, inventory planning, recipe costing, waste tracking, order predictions, and an AI assistant (CostPilot) that answers your business questions using local LLM inference.

**Contact:** 940-612-9836 | 940-612-9045 | CurbsideCare940@gmail.com  
**GitHub:** [TechsNtheCity940/MarginMise](https://github.com/TechsNtheCity940/MarginMise)

---

## 🚀 Quick Start

### Windows

1. Download the latest release from the [Releases](https://github.com/TechsNtheCity940/MarginMise/releases) page, or build from source.
2. Extract the zip and run `install_windows.bat`.
3. Launch MarginMise from the Start Menu shortcut or by running `run_gui.bat`.

### macOS / Linux

```bash
git clone https://github.com/TechsNtheCity940/MarginMise.git
cd MarginMise/restaurant-cost-controller
./install_linux_macos.sh
./run_gui.sh
```

---

## 📦 What It Does

### 1. Invoice Processing
- Drop invoice PDFs/images/CSV files into your desktop **Inbox** folder
- Automatic classification, OCR extraction (RapidOCR + Tesseract), and data entry
- Self-healing: tries multiple parsing strategies before flagging for review
- Vendor recognition and duplicate detection

### 2. Recipe Costing
- Import recipe spreadsheets (CSV/XLSX) listing ingredients per menu item
- Calculates recipe cost + food cost percentage using current inventory prices
- **Price recommendation engine**: suggests minimum 3× recipe cost for menu pricing

### 3. Inventory Planning
- Monthly inventory counts update on-hand estimates automatically
- Smart order predictions using demand forecasting with weekday/weather/event multipliers
- Margin memory learns from past manager overrides to improve predictions

### 4. Waste Tracking
- Log waste events by item, reason, shift
- Waste cost attribution to menu items
- Pattern detection across time periods

### 5. CostPilot — Local AI Assistant
- Ask questions like: *"What item sold the most last month?"*, *"What decisions did I make and what was the outcome?"*, *"How much did we spend on Ground Beef?"*
- Uses **llama.cpp + LFM2.5** (CPU-only, 250MB model) — installed silently during first-run setup
- Falls back to deterministic SQL answers when the LLM is unavailable
- Every answer includes **source citations** for traceability

### 6. Margin Memory
- Captures every manager order decision with full context (weekday, weather, events, inventory level)
- Evaluates decisions against actual outcomes (usage, stockouts, ending inventory)
- Learns from past overrides to generate smarter order predictions
- Tracks patterns across similar demand scenarios

### 7. Dashboard & Reporting
- Real-time profit & loss dashboard
- Food cost percentage tracking
- Waste trend analysis
- Export to Excel/PDF for accountant review

---

## 🏗️ Building from Source

### Python Environment Setup

```bash
cd restaurant-cost-controller
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate     # Windows

pip install -r requirements.txt
```

### Required System Dependencies

| Dependency | Purpose |
|---|---|
| **Python 3.12** | Core runtime |
| **Tesseract OCR** | Text extraction from scanned documents (installed by install scripts) |
| **RapidOCR (ONNX Runtime)** | Local OCR engine (installed via pip) |
| **llama.cpp + LFM2.5 model** | CostPilot AI assistant (~250MB, installed by install scripts) |

The install scripts (`install_linux_macos.sh` / `install_windows.bat`) handle all of these automatically with **graceful degradation** — if any download fails, the program still works with deterministic (non-AI) mode.

### Running

```bash
# GUI
python launch_gui.py

# Or directly
python restaurant_cost_gui.py
```

### Data Location

MarginMise creates a workspace directory for all data:
- **Linux/macOS:** `~/.local/share/MarginMise/`
- **Windows:** `%LOCALAPPDATA%\MarginMise\`

This includes the SQLite database, extracted documents, OCR cache, and the local AI model.

---

## 📦 Building the Windows .exe (Standalone Executable)

MarginMise can be packaged as a standalone Windows executable so managers don't need Python installed. Here's how:

### Prerequisites (on the build machine)
- Windows 10/11
- Python 3.12 (https://python.org)
- Node.js (optional, for icon generation)

### Build Steps

1. **Install build dependencies:**
   ```cmd
   cd restaurant-cost-controller
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   pip install pyinstaller
   ```

2. **Copy the logo icon** to the project root:
   The build uses `assets/app_icon_256.png` as the application icon (already included).

3. **Build with PyInstaller:**
   ```cmd
   pyinstaller --onefile --windowed ^
     --name "MarginMise" ^
     --icon assets/app_icon_256.png ^
     --add-data "assets;assets" ^
     --hidden-import local_ai ^
     --hidden-import local_ocr ^
     --hidden-import manager_chat ^
     --hidden-import invoice_pipeline ^
     --hidden-import bulk_ingestion ^
     --hidden-import recipe_costing ^
     --hidden-import margin_memory ^
     --hidden-import inventory_planning ^
     --hidden-import phase2_features ^
     --hidden-import phase3_features ^
     --hidden-import operational_controls ^
     --hidden-import excel_io ^
     --hidden-import dashboard_service ^
     --hidden-import dashboard_widgets ^
     --hidden-import src.theme ^
     launch_gui.py
   ```

   **Or use the provided build script:**
   ```cmd
   build_exe.bat
   ```

4. **Find the output:** The executable will be at `dist/MarginMise.exe`

5. **Distribute:** Copy `dist/MarginMise.exe` to any Windows PC. On first run, it will:
   - Extract itself to `%LOCALAPPDATA%\MarginMise`
   - Download Tesseract OCR (silent)
   - Download llama.cpp + LFM2.5 model (silent, ~250MB)
   - No internet required after initial setup

### PyInstaller Spec File

A production-ready `.spec` file is included at `marginmise.spec`:

```ini
# To build:
pyinstaller marginmise.spec
```

The spec file includes:
- One-file executable with embedded Python runtime
- Windowed mode (no console popup)
- All Python modules explicitly imported
- Assets (icons, logos) bundled as data files

---

## 🎨 Branding & Color Scheme

| Color | Hex | Usage |
|---|---|---|
| **Midnight Navy** | `#0B1F33` | Left navigation, dark headers, app icon background |
| **Ocean Teal** | `#0F6B78` | Active navigation, chart lines, secondary actions |
| **Burgundy** | `#7A1F3D` | Brand accents, selected financial metrics, M logo (left half) |
| **Fire Orange** | `#F97316` | Primary action buttons, CTA elements, M logo (dot accent) |
| **Frost White** | `#F8FAFC` | App background, light mode surfaces |
| **Charcoal** | `#1E293B` | Primary text |

### Logo Assets

The `assets/` directory contains:
- `app_icon_*.png` — Application icons (16px to 1024px) for Windows taskbar, macOS dock, file associations
- `favicon.ico` — Multi-size ICO for browser/web
- `loading_screen_512x288.png` — Splash screen shown during startup + AI model loading
- `desktop_800x600.png` — Desktop wallpaper/shortcut image
- `MarginMiseLogo.png` — Full 1536×1024 high-res logo
- `color_palette.json` — Developer reference for all colors

### Logo Design

The MarginMise logo features:
- **Stylized "MM"** — Two overlapping geometric shapes (burgundy left M, teal right M) forming a modern, professional mark
- **Orange dot** — Represents the "fire" of restaurant operations and the energy of the kitchen
- **Navy background** — Professional, stable, trustworthy
- **Clean white inner space** — Clarity and precision

---

## 📖 Usage Guide

### Day-to-Day Workflow

1. **Drop invoices** into your desktop `MarginMise/Inbox/` folder — the program auto-processes them
2. **Enter inventory counts** via the mobile-friendly inventory module or import a CSV
3. **Import POS sales** exports from your POS system
4. **Review recipe costs** in the Recipe Costing module
5. **Ask CostPilot** questions by clicking the chat icon

### Year-End Reports

- **Profit & Loss:** Dashboard → Export P&L → "Export to Excel"
- **Food Cost:** Recipe Costing → "Export Recipe Costs"
- **Waste Report:** Waste Tracking → "Export Waste Log"
- **Tax Documentation:** Invoices module → "Export All Processed Invoices"

---

## 🆘 Support

- **Phone:** 940-612-9836
- **Email:** CurbsideCare940@gmail.com
- **GitHub Issues:** [Report a bug or request a feature](https://github.com/TechsNtheCity940/MarginMise/issues)

---

## 📄 License

Proprietary — Developed by [Curbside Care](https://curbsidecare940.github.io) for independent restaurant operations.

**Talk Trash To Us...Seriously....We love it!**
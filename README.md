# MarginMise — Restaurant Cost Management

MarginMise is an all-in-one restaurant cost management system that runs entirely on your local machine — no cloud, no monthly fees, no internet required after install. It handles invoice processing, inventory planning, recipe costing, waste tracking, order predictions, and an AI assistant (CostPilot) that answers your business questions using local LLM inference.

**GitHub:** [TechsNtheCity940/MarginMise](https://github.com/TechsNtheCity940/MarginMise)

---

## 🚀 Quick Start

### Windows

1. Download the latest release from the [Releases](https://github.com/TechsNtheCity940/MarginMise/releases) page, or build from source.
2. Extract the zip and run `MarginMise.exe` or `install_windows.bat`.
3. The first run will:
   - Install Python if needed
   - Create a virtual environment
   - Install all dependencies
   - Silently install Tesseract OCR
   - Download the local AI model
4. Subsequent launches start immediately.
5. To distribute: copy `dist/MarginMise.exe` to any Windows PC. No Python installation is needed on the target machine.

### macOS / Linux

```bash
git clone https://github.com/TechsNtheCity940/MarginMise.git
cd MarginMise
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
- Uses **llama.cpp + LFM2.5** (CPU-only, ~250MB model) — installed silently during first-run setup
- Falls back to deterministic SQL answers when the LLM is unavailable
- Every answer includes **source citations** for traceability

### 6. Margin Memory
- Captures every manager order decision with full context (weekday, weather, events, inventory level)
- Evaluates decisions against actual outcomes (usage, stockouts, ending inventory)
- Learns from past overrides to generate smarter order predictions
- Tracks patterns across similar demand scenarios
- **Scorecard**: shows managers how the system is learning over time

### 7. Dashboard & Reporting
- Real-time profit & loss dashboard
- Food cost percentage tracking
- Waste trend analysis
- Export to Excel/PDF for accountant review

### 8. Upcoming Events
- Input upcoming events: concerts, holidays, weather, construction, promotions
- Events influence demand forecasts automatically
- Margin memory learns how each location responds to different event types

---

## 🏗️ Building from Source

### Prerequisites

| Dependency | Purpose | Install Method |
|---|---|---|
| **Python 3.11+** | Core runtime | Installer or winget |
| **Tesseract OCR** | Text extraction from scanned documents | Silent install via `local_ocr.py ensure --install-tesseract` |
| **RapidOCR + ONNX Runtime** | Local OCR engine | `pip install -r requirements.txt` |
| **llama.cpp + LFM2.5 model** | CostPilot AI assistant (~250MB) | Silent download via `local_ai.py ensure` |

### Setup

```bash
git clone https://github.com/TechsNtheCity940/MarginMise.git
cd MarginMise

# Create virtual environment
python3 -m venv .venv

# Activate virtual environment
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

### First-Run Setup

```bash
# Install Tesseract OCR silently (Windows)
python local_ocr.py ensure --install-tesseract

# Install local AI runtime and model
python local_ai.py ensure

# Launch the GUI
python launch_gui.py
```

### Windows Installer

For end users, run `install_windows.bat` — it will:
1. Find or install Python 3.11+
2. Create a virtual environment
3. Install all Python dependencies
4. Silently install Tesseract OCR
5. Download llama.cpp + LFM2.5 model
6. Create desktop/start menu shortcuts

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

MarginMise can be packaged as a standalone Windows executable so managers don't need Python installed.

### Build Steps

1. **Install build dependencies:**
   ```cmd
   pip install pyinstaller
   ```

2. **Build with PyInstaller:**
   ```cmd
   build_exe.bat
   ```

3. **Find the output:** The executable will be at `dist/MarginMise.exe`

4. **Distribute:** Copy `dist/MarginMise.exe` to any Windows PC. On first run, it will:
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

---

## 📖 Usage Guide

### Day-to-Day Workflow

1. **Drop invoices** into your desktop `MarginMise/Inbox/` folder — the program auto-processes them
2. **Enter inventory counts** via the mobile-friendly inventory module or import a CSV
3. **Import POS sales** exports from your POS system
4. **Review recipe costs** in the Recipe Costing module
5. **Ask CostPilot** questions by clicking the chat icon
6. **Add upcoming events** to improve demand forecasting

### Year-End Reports

- **Profit & Loss:** Dashboard → Export P&L → "Export to Excel"
- **Food Cost:** Recipe Costing → "Export Recipe Costs"
- **Waste Report:** Waste Tracking → "Export Waste Log"
- **Tax Documentation:** Invoices module → "Export All Processed Invoices"

---

## 🆘 Support

- **GitHub Issues:** [Report a bug or request a feature](https://github.com/TechsNtheCity940/MarginMise/issues)

---

## 📄 License

Proprietary — Developed for independent restaurant operations.

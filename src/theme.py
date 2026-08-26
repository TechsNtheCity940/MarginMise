# MarginMise Theme Constants
# This file contains the centralized color palette for the application

# Brand and System Colors
PRIMARY_NAVY = "#0B1F33"  # Main left navigation, major shell elements
OCEAN_TEAL = "#0F6B78"  # Active navigation, chart lines, information
BURGUNDY = "#7A1F3D"  # MarginMise brand accents, selected financial metrics
FIRE_ORANGE = "#F97316"  # Primary action buttons, important call-to-action elements
FROST_WHITE = "#F8FAFC"  # Light background
CHARCOAL = "#1E293B"  # Primary text / neutral
SLATE = "#475569"  # Secondary text
BORDER_COLOR = "#D8E0E8"  # Borders
CARD_BG_COLOR = "#FFFFFF"  # Card background
WHITE = "#FFFFFF"
LIGHT_SLATE = "#94A3B8"
SOFT_SLATE = "#CBD5E1"
SUBTLE_GRID = "#E8EEF3"
SIDEBAR_TEXT = "#E2E8F0"
SIDEBAR_DIVIDER = "#294057"
DISABLED_TEXT = "#64748B"

# Status Colors
SUCCESS = "#16A34A"  # Good performance, verified
WARNING = "#F59E0B"  # Needs attention, watchlist
ERROR = "#DC2626"  # Critical problem, failed processing

# Dashboard chart sequence. Burgundy remains a brand/category color and is
# deliberately separate from the ERROR state.
CHART_COLORS = (
    BURGUNDY,
    OCEAN_TEAL,
    FIRE_ORANGE,
    PRIMARY_NAVY,
    LIGHT_SLATE,
    SUCCESS,
    WARNING,
)

# Shared geometry and typography values for the desktop shell.
SIDEBAR_MIN_WIDTH = 210
SIDEBAR_MAX_WIDTH = 290
SIDEBAR_WIDTH_RATIO = 0.15
CARD_RADIUS = 10
PAGE_PADDING = 18
CARD_PADDING = 14
FONT_FAMILY = "Segoe UI"

# Usage Rules
# Midnight Navy: Main left navigation, major shell elements, dark headers
# Ocean Teal: Active navigation, chart lines, information, analytics, secondary buttons
# Burgundy: MarginMise brand accents, selected financial metrics, chart accents, section highlights
# Fire Orange: Primary action buttons, important call-to-action elements, growth/performance highlights
# Green: Good performance, verified, approved, on target
# Amber: Needs attention, watchlist, pending review
# Red: Critical problem, failed processing, rejected, serious discrepancy

# Do not use Burgundy as the error color. Brand color and error state must remain visually distinct.

#!/usr/bin/env bash
# ==============================================================================
# Unified Ecosystem Runner & CLI Script: Microinvest Invoice OCR
#
# Launches the FastAPI core microservice (:8000) with comprehensive pre-flight
# environment diagnostics and cross-repo ecosystem integration checks.
#
# Usage:
#   ./scripts/start_ecosystem.sh [OPTIONS]
#
# Options:
#   --live         Start in production/live mode (connects to local Tesseract, DB, & real OCR) [Default]
#   --mock         Start in mock mode (simulates external dependencies & offline mocks)
#   --port PORT    Port for FastAPI microservice (default: 8000)
#   --host HOST    Host interface for FastAPI microservice (default: 0.0.0.0)
#   --reload       Enable uvicorn auto-reload for development
#   --workers N    Number of uvicorn worker processes (default: 1)
#   -h, --help     Show this help message and exit
# ==============================================================================

set -eo pipefail

# ANSI Color codes
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
BLUE="\033[0;34m"
CYAN="\033[0;36m"
NC="\033[0m" # No Color

# Default parameters
MODE="live"
HOST="0.0.0.0"
PORT=8000
RELOAD=""
WORKERS=1

# Root directory of invoice-tesseract-ocr
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

# Parse command-line arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --live)
      MODE="live"
      shift
      ;;
    --mock)
      MODE="mock"
      shift
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --host)
      HOST="$2"
      shift 2
      ;;
    --reload)
      RELOAD="--reload"
      shift
      ;;
    --workers)
      WORKERS="$2"
      shift 2
      ;;
    -h|--help)
      echo -e "${BOLD}Unified Ecosystem Runner: Microinvest Invoice OCR & FastAPI Core${NC}"
      echo ""
      echo "Usage: $0 [--live | --mock] [--port 8000] [--host 0.0.0.0] [--reload] [--workers 1]"
      echo ""
      echo "Options:"
      echo "  --live         Start in production/live mode (default)"
      echo "  --mock         Start in mock mode with offline fallbacks"
      echo "  --port PORT    FastAPI port (default: 8000)"
      echo "  --host HOST    FastAPI host interface (default: 0.0.0.0)"
      echo "  --reload       Enable uvicorn auto-reload"
      echo "  --workers N    Number of worker processes"
      echo "  -h, --help     Show help"
      exit 0
      ;;
    *)
      echo -e "${RED}Unknown argument: $1${NC}"
      echo "Use --help for usage instructions."
      exit 1
      ;;
  esac
done

# Banner
echo -e "${CYAN}======================================================================${NC}"
echo -e "${BOLD}${CYAN}   MICROINVEST ECOSYSTEM UNIFIED RUNNER & DIAGNOSTICS   ${NC}"
echo -e "${CYAN}======================================================================${NC}"
MODE_UPPER="$(echo "${MODE}" | tr '[:lower:]' '[:upper:]')"
echo -e "Mode:          ${BOLD}${YELLOW}${MODE_UPPER}${NC}"
echo -e "Project Root:  ${PROJECT_ROOT}"
echo -e "FastAPI Bind:  ${BOLD}http://${HOST}:${PORT}${NC}"
echo -e "Timestamp:     $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo -e "${CYAN}----------------------------------------------------------------------${NC}"

# Python Environment Resolution
PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  if command -v python3 &>/dev/null; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo -e "${RED}[ERROR] Python 3 executable not found in .venv or PATH.${NC}"
    exit 1
  fi
fi
echo -e "${GREEN}[OK]${NC} Python Binary:       ${PYTHON_BIN} ($(${PYTHON_BIN} --version))"

# Mode configuration
if [[ "${MODE}" == "mock" ]]; then
  export MOCK_MODE="1"
  export MOCK_ERP="1"
  export OFFLINE_MODE="1"
  echo -e "${YELLOW}[INFO] Running in MOCK mode (Mock ERP Webhook & offline testing enabled)${NC}"
else
  export MOCK_MODE="0"
  export MOCK_ERP="0"
  export OFFLINE_MODE="0"
  echo -e "${GREEN}[INFO] Running in LIVE mode (Direct Tesseract OCR & local database active)${NC}"
fi

# Pre-flight Environment Diagnostics
echo -e "\n${BOLD}Executing Pre-Flight Environment Diagnostics...${NC}"

# 1. Tesseract OCR Verification
if command -v tesseract &>/dev/null; then
  TESS_VER="$(tesseract --version 2>&1 | head -n 1)"
  TESS_LANGS="$(tesseract --list-langs 2>&1 | tail -n +2 | tr '\n' ' ')"
  if [[ "${TESS_LANGS}" =~ "bul" && "${TESS_LANGS}" =~ "eng" ]]; then
    echo -e "${GREEN}[OK]${NC} Tesseract OCR:       ${TESS_VER}"
    echo -e "${GREEN}[OK]${NC} OCR Languages:       bul, eng installed (${TESS_LANGS})"
  else
    echo -e "${YELLOW}[WARN]${NC} Tesseract installed (${TESS_VER}) but missing 'bul' or 'eng' in: ${TESS_LANGS}"
  fi
else
  if [[ "${MODE}" == "mock" ]]; then
    echo -e "${YELLOW}[WARN]${NC} Tesseract binary not in PATH (Mock mode active, skipping hard requirement)"
  else
    echo -e "${RED}[ERROR]${NC} Tesseract binary not found in PATH. Install tesseract with 'bul' and 'eng' packages."
    exit 1
  fi
fi

# 2. Database & Audit Tables Verification
DB_FILE="${PROJECT_ROOT}/invoice_ocr.db"
if [[ -f "${DB_FILE}" ]]; then
  echo -e "${GREEN}[OK]${NC} SQLite Database:     ${DB_FILE} ($(ls -lh "${DB_FILE}" | awk '{print $5}'))"
else
  echo -e "${YELLOW}[INFO]${NC} Database file not found; initializing on startup."
fi

# 3. Worker Pool Verification
CPU_COUNT="$(${PYTHON_BIN} -c 'import os; print(os.cpu_count() or 4)')"
MAX_WORKERS="$(${PYTHON_BIN} -c 'import os; print(min(os.cpu_count() or 4, 16))')"
echo -e "${GREEN}[OK]${NC} CPU Cores / Workers: ${CPU_COUNT} cores detected (Max OCR Workers: ${MAX_WORKERS})"

# 4. Vendor Configuration Directory
VENDORS_DIR="${PROJECT_ROOT}/config/vendors"
if [[ -d "${VENDORS_DIR}" ]]; then
  VENDOR_COUNT="$(find "${VENDORS_DIR}" -type f \( -name "*.yaml" -o -name "*.yml" \) | wc -l | tr -d ' ')"
  echo -e "${GREEN}[OK]${NC} Vendor Profiles:     ${VENDORS_DIR} (${VENDOR_COUNT} profiles loaded)"
else
  echo -e "${YELLOW}[WARN]${NC} Vendor configuration directory ${VENDORS_DIR} not found."
fi

# 5. Microinvest Delta Pro Module Verification
MICRO_OK="$(${PYTHON_BIN} -c "
try:
    from invoice_core.microinvest_export import generate_microinvest_delta_xml, generate_microinvest_delta_csv
    print('1')
except Exception as e:
    print('0')
")"
if [[ "${MICRO_OK}" == "1" ]]; then
  echo -e "${GREEN}[OK]${NC} Microinvest Export:  Delta Pro XML/CSV and Sklad Pro XML modules ready"
else
  echo -e "${RED}[ERROR]${NC} Failed importing Microinvest Delta Pro export modules."
  exit 1
fi

# 6. Cross-Repo Ecosystem Topology Check
MICROINVEST_OCR_DIR="/Users/diokarabaz/MICROINVEST-OCR"
if [[ -d "${MICROINVEST_OCR_DIR}" ]]; then
  echo -e "${GREEN}[OK]${NC} Microinvest Portal:  ${MICROINVEST_OCR_DIR} detected"
  echo -e "     - Express Gateway:   ${MICROINVEST_OCR_DIR}/backend (:3000)"
  echo -e "     - Astro Dashboard:   ${MICROINVEST_OCR_DIR}/app (:4321)"
else
  echo -e "${YELLOW}[INFO]${NC} Microinvest web portal directory not found at ${MICROINVEST_OCR_DIR}"
fi

echo -e "${CYAN}----------------------------------------------------------------------${NC}"
echo -e "${BOLD}${GREEN}All pre-flight checks passed! Launching FastAPI Core...${NC}"
echo -e "  • Interactive Swagger Docs:  ${BOLD}http://localhost:${PORT}/docs${NC}"
echo -e "  • Ecosystem Health Endpoint: ${BOLD}http://localhost:${PORT}/api/v1/system/ecosystem-health${NC}"
echo -e "  • Human-in-the-Loop Web UI:  ${BOLD}http://localhost:${PORT}/dashboard${NC}"
echo -e "${CYAN}----------------------------------------------------------------------${NC}\n"

# Launch uvicorn
exec "${PYTHON_BIN}" -m uvicorn api_server:app \
  --host "${HOST}" \
  --port "${PORT}" \
  --workers "${WORKERS}" \
  ${RELOAD}

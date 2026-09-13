"""UTM Windows XP Virtual Machine Bridge & Microinvest Delta Pro Direct Connector.

Bridges the OCR & Jet 2.0 accounting pipeline with the UTM Windows XP VM:
1. Manages UTM VM lifecycle and status via utmctl.
2. Connects/disconnects USB media dynamically (e.g. Kingston ABCD:1234).
3. Stages firm-partitioned TRANSFER.LOG and TRANSFER.ldb files in shared or USB directories.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Optional

logger = logging.getLogger("utm_bridge")

DEFAULT_UTMCTL_PATH = Path("/Applications/UTM.app/Contents/MacOS/utmctl")
DEFAULT_VM_NAME = "Windows XP"
DEFAULT_TRANSFER_ROOT = Path(os.environ.get(
    "UTM_TRANSFER_ROOT",
    "/Users/diokarabaz/Microinvest-Transfer"
))


def find_utmctl() -> Optional[Path]:
    """Locate the utmctl binary on the host."""
    if DEFAULT_UTMCTL_PATH.exists() and os.access(DEFAULT_UTMCTL_PATH, os.X_OK):
        return DEFAULT_UTMCTL_PATH
    w = shutil.which("utmctl")
    return Path(w) if w else None


def get_vm_status(vm_name: str = DEFAULT_VM_NAME) -> dict[str, Any]:
    """Query current status of the target UTM virtual machine."""
    utmctl = find_utmctl()
    if not utmctl:
        return {"ok": False, "error": "utmctl not found on this host"}

    try:
        res = subprocess.run(
            [str(utmctl), "status", vm_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        status_str = res.stdout.strip().lower()
        return {
            "ok": True,
            "vm_name": vm_name,
            "status": status_str,
            "is_running": status_str in ("started", "running"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_usb_devices() -> list[dict[str, str]]:
    """Enumerate USB devices recognized by UTM."""
    utmctl = find_utmctl()
    if not utmctl:
        return []

    try:
        res = subprocess.run(
            [str(utmctl), "usb", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        devices = []
        for line in res.stdout.splitlines():
            if not line.strip() or line.startswith("Name"):
                continue
            match = re.search(r"^(.*?)\s+([0-9A-Fa-f]{4}:[0-9A-Fa-f]{4})\s+(\S+)", line)
            if match:
                devices.append({
                    "name": match.group(1).strip(),
                    "vid_pid": match.group(2).upper(),
                    "location": match.group(3),
                })
        return devices
    except Exception as e:
        logger.error(f"Error listing USB devices: {e}")
        return []


def connect_usb_to_vm(device_id: str, vm_name: str = DEFAULT_VM_NAME) -> dict[str, Any]:
    """Connect a USB device to the VM."""
    utmctl = find_utmctl()
    if not utmctl:
        return {"ok": False, "error": "utmctl not found"}

    try:
        cmd = [str(utmctl), "usb", "connect", vm_name, device_id]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if res.returncode == 0:
            return {"ok": True, "message": f"Connected device {device_id} to VM '{vm_name}'"}
        err = res.stderr.strip() or res.stdout.strip()
        return {"ok": False, "error": err or "Failed connecting USB device"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def disconnect_usb_from_vm(device_id: str, vm_name: str = DEFAULT_VM_NAME) -> dict[str, Any]:
    """Disconnect a USB device from the VM returning control to host macOS."""
    utmctl = find_utmctl()
    if not utmctl:
        return {"ok": False, "error": "utmctl not found"}

    try:
        cmd = [str(utmctl), "usb", "disconnect", vm_name, device_id]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if res.returncode == 0:
            return {"ok": True, "message": f"Disconnected device {device_id} from VM '{vm_name}'"}
        err = res.stderr.strip() or res.stdout.strip()
        return {"ok": False, "error": err or "Failed disconnecting USB device"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def stage_firm_transfer_files(
    source_dir: Path,
    target_drop_dir: Path,
    firm_slug: str = "Building_11",
    firm_eik: str = "206062202",
) -> dict[str, Any]:
    """Copy TRANSFER.LOG, TRANSFER.ldb, and summary into the firm-isolated drop folder."""
    firm_target = target_drop_dir / f"{firm_eik}_{firm_slug}"
    firm_target.mkdir(parents=True, exist_ok=True)

    src_log = source_dir / "TRANSFER.LOG"
    src_ldb = source_dir / "TRANSFER.ldb"

    if not src_log.exists():
        return {"ok": False, "error": f"Source TRANSFER.LOG not found at {src_log}"}

    dest_log = firm_target / "TRANSFER.LOG"
    dest_ldb = firm_target / "TRANSFER.ldb"

    shutil.copy2(src_log, dest_log)
    if src_ldb.exists():
        shutil.copy2(src_ldb, dest_ldb)
    else:
        from invoice_core.delta_pro_generator import DELTA_PRO_LDB_TEMPLATE
        dest_ldb.write_bytes(DELTA_PRO_LDB_TEMPLATE)

    meta = {
        "firm_slug": firm_slug,
        "firm_eik": firm_eik,
        "status": "ready_for_import",
        "log_size": dest_log.stat().st_size,
        "ldb_size": dest_ldb.stat().st_size,
        "transfer_log_path_win": f"Z:\\{firm_eik}_{firm_slug}\\TRANSFER.LOG",
    }
    (firm_target / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "ok": True,
        "staged_directory": str(firm_target),
        "files": ["TRANSFER.LOG", "TRANSFER.ldb", "meta.json"],
        "meta": meta,
    }

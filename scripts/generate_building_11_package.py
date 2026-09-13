import os
import shutil
import struct
from pathlib import Path

BASE_DIR = Path("/Users/diokarabaz/orca/projects/invoice-tessearct-ocr")
CANONICAL_LOG = BASE_DIR / "comparison_export" / "TRANSFER.LOG"
CANONICAL_LDB = BASE_DIR / "comparison_export" / "TRANSFER.ldb"

DEST_DIRS = [
    Path("/Volumes/NO NAME/Building_11"),
    Path("/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/Building_11"),
    Path("/Users/diokarabaz/.gemini/antigravity-cli/brain/0a7f4473-555c-4082-b34f-d1f3912dfbd5/Building_11"),
]

def main():
    assert CANONICAL_LOG.exists(), f"Canonical LOG missing: {CANONICAL_LOG}"
    assert CANONICAL_LDB.exists(), f"Canonical LDB missing: {CANONICAL_LDB}"
    
    log_bytes = CANONICAL_LOG.read_bytes()
    ldb_bytes = CANONICAL_LDB.read_bytes()
    
    assert len(log_bytes) == 65536, f"Invalid LOG size: {len(log_bytes)}"
    assert len(ldb_bytes) == 64, f"Invalid LDB size: {len(ldb_bytes)}"
    
    print(f"Canonical TRANSFER.LOG size: {len(log_bytes)} bytes")
    print(f"Canonical TRANSFER.ldb size: {len(ldb_bytes)} bytes")
    
    for d in DEST_DIRS:
        if not d.parent.exists():
            print(f"Skipping {d} (parent directory not found / unmounted)")
            continue
            
        print(f"\nUpdating {d} ...")
        d.mkdir(parents=True, exist_ok=True)
        
        target_log = d / "TRANSFER.LOG"
        target_ldb = d / "TRANSFER.ldb"
        
        target_log.write_bytes(log_bytes)
        target_ldb.write_bytes(ldb_bytes)
        
        print(f"  - Deployed: {target_log} ({target_log.stat().st_size} bytes)")
        print(f"  - Deployed: {target_ldb} ({target_ldb.stat().st_size} bytes)")
        
        # Clean up any loose non-mdb/ldb files (CSV, XML, SQL, txt) but keep subdirectories like 'ЕКСПОРТ ЗА СРАВНЕНИЕ'
        for item in d.iterdir():
            if item.is_file() and item.name not in ["TRANSFER.LOG", "TRANSFER.ldb"]:
                print(f"  - Removing obsolete file: {item.name}")
                item.unlink()
                
        files = [f.name for f in d.iterdir() if f.is_file() and not f.name.startswith(".")]
        print(f"  - Files in {d.name}: {sorted(files)}")
        assert sorted(files) == ["TRANSFER.LOG", "TRANSFER.ldb"], f"Unexpected files in {d}: {files}"
        
    print("\nSUCCESS: Canonical Delta Pro transfer files deployed to all locations!")

if __name__ == "__main__":
    main()

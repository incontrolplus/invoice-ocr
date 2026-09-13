#!/usr/bin/env python3
import json
import os
from pathlib import Path

TARGET_DIR = Path("/Volumes/KINGSTON/_02_БИЛДИНГ_11_ООД")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = PROJECT_ROOT / "scratch_kingston_analysis.json"

def main():
    print(f"Executing sequential rename on {TARGET_DIR}...")
    files = [p for p in TARGET_DIR.glob("*.pdf") if not p.name.startswith("._")]
    files.sort(key=lambda p: int(p.name.split("_")[0]) if p.name.split("_")[0].isdigit() else 999)

    shift_list = []
    for p in files:
        parts = p.name.split("_", 1)
        if parts[0].isdigit():
            num = int(parts[0])
            if num >= 11:
                target_name = f"{num - 1}_{parts[1]}"
                shift_list.append((num, p, TARGET_DIR / target_name))

    # Must rename in ascending order 11 -> 10, 12 -> 11, etc.
    shift_list.sort(key=lambda x: x[0])

    print(f"Renaming {len(shift_list)} files...")
    for num, src_path, dst_path in shift_list:
        src_dot = TARGET_DIR / f"._{src_path.name}"
        dst_dot = TARGET_DIR / f"._{dst_path.name}"

        # Rename main file
        src_path.rename(dst_path)

        # Rename ._ file if exists
        if src_dot.exists():
            src_dot.rename(dst_dot)

        print(f"Renamed: {src_path.name} -> {dst_path.name}")

    print("All files renamed successfully!")

    # Verify new sequence
    new_files = [p for p in TARGET_DIR.glob("*.pdf") if not p.name.startswith("._")]
    nums = sorted([int(p.name.split("_")[0]) for p in new_files if p.name.split("_")[0].isdigit()])
    print(f"New file count: {len(nums)}")
    print(f"Range: min={min(nums)}, max={max(nums)}")
    missing = [i for i in range(1, len(nums) + 1) if i not in nums]
    print(f"Missing in 1..{len(nums)}: {missing}")
    assert len(nums) == 61
    assert min(nums) == 1
    assert max(nums) == 61
    assert not missing, f"Sequence has gaps: {missing}"

    # Update cache file
    if CACHE_FILE.exists():
        print(f"Updating cache: {CACHE_FILE}")
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        for item in data:
            old_name = item.get("file_name", "")
            parts = old_name.split("_", 1)
            if parts[0].isdigit():
                num = int(parts[0])
                if num >= 11:
                    new_name = f"{num - 1}_{parts[1]}"
                    item["file_name"] = new_name
                    item["pdf_path"] = str(TARGET_DIR / new_name)

        data.sort(key=lambda x: int(x["file_name"].split("_")[0]) if x["file_name"].split("_")[0].isdigit() else 999)

        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("Cache updated successfully!")

if __name__ == "__main__":
    main()

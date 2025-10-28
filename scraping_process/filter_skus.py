#!/usr/bin/env python3
"""
Filter SKUs - Remove lines with less than 6 characters, duplicates, and clean format
"""


def filter_skus():
    input_file = "files/skus.txt"

    # Read all lines
    with open(input_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Filter lines with 6+ characters (excluding whitespace)
    filtered_lines = []
    removed_count = 0
    cleaned_count = 0
    letters_only_count = 0
    space_replaced_count = 0

    for line in lines:
        stripped_line = line.strip()

        # Replace spaces with dashes
        # Example: "107715 03" becomes "107715-03"
        if " " in stripped_line:
            original_line = stripped_line
            stripped_line = stripped_line.replace(" ", "-")
            space_replaced_count += 1
            print(f"🔄 Space replaced: '{original_line}' → '{stripped_line}'")

        # Remove second dash and everything after it
        # Example: "446103-8000-XL" becomes "446103-8000"
        dash_count = stripped_line.count("-")
        if dash_count >= 2:
            # Find the second dash and remove everything from there
            first_dash = stripped_line.find("-")
            second_dash = stripped_line.find("-", first_dash + 1)
            if second_dash != -1:
                original_line = stripped_line
                stripped_line = stripped_line[:second_dash]
                cleaned_count += 1
                print(f"🔧 Cleaned: '{original_line}' → '{stripped_line}'")

        # Check if SKU contains only letters (no numbers)
        if stripped_line.isalpha():
            letters_only_count += 1
            print(f"🔤 Removed letters-only SKU: '{stripped_line}'")
            continue

        # Filter by length (6+ characters)
        if len(stripped_line) >= 6:
            filtered_lines.append(stripped_line)
        else:
            removed_count += 1

    # Remove duplicates while preserving order
    seen = set()
    unique_lines = []
    duplicate_count = 0

    for line in filtered_lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)
        else:
            duplicate_count += 1
            print(f"🔄 Removed duplicate: '{line}'")

    # Write UNIQUE lines back (this was the bug - was writing filtered_lines instead!)
    with open(input_file, "w", encoding="utf-8") as f:
        for line in unique_lines:
            f.write(line + "\n")

    print(f"✅ Filtering complete!")
    print(f"📊 Original lines: {len(lines)}")
    print(f"🔄 Lines with spaces replaced: {space_replaced_count}")
    print(f"🔧 Lines cleaned (removed 2nd dash): {cleaned_count}")
    print(f"🔤 Lines removed (letters only): {letters_only_count}")
    print(f"🔄 Duplicates removed: {duplicate_count}")
    print(f"📊 Kept lines (6+ chars): {len(filtered_lines)}")
    print(f"📊 Removed lines (<6 chars): {removed_count}")
    print(f"🎯 Final unique SKUs: {len(unique_lines)}")

    # Show sample of kept lines
    if unique_lines:
        print(f"\n📋 Sample of final SKUs:")
        for i, sku in enumerate(unique_lines[:10]):
            print(f"   {sku} (length: {len(sku)})")
        if len(unique_lines) > 10:
            print(f"   ... and {len(unique_lines) - 10} more")


if __name__ == "__main__":
    filter_skus()

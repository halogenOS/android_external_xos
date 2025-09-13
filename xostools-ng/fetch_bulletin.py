#!/usr/bin/env python3
"""
Android Security Bulletin fetcher that extracts patch information as JSON.
Usage: python fetch_bulletin.py 2025-09-01
"""

import sys
import json
import re
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup


def parse_git_url(url, android_versions=None):
    """Parse Android git URL to extract repo and commit info."""
    # Example: https://android.googlesource.com/platform/frameworks/base/+/ed39b7c3895c8c63a1ccdbcc9783a2d3ca15127f
    pattern = r'https://android\.googlesource\.com/([^/]+(?:/[^/]+)*)/\+/([a-f0-9]+)'
    match = re.match(pattern, url)

    if match:
        repo_path = match.group(1)
        commit_ref = match.group(2)

        patch_info = {
            "url": url,
            "repo": f"https://android.googlesource.com/{repo_path}",
            "remote": "aosp",
            "name": repo_path,
            "ref": commit_ref
        }

        if android_versions:
            patch_info["android_versions"] = android_versions

        return patch_info
    return None


def parse_vulnerability_table(table, current_component):
    """Parse a vulnerability table to extract patches with Android version information."""
    patches = []

    # Find header row
    header_row = table.find('tr')
    if not header_row:
        return patches

    headers = [cell.get_text(strip=True) for cell in header_row.find_all(['th', 'td'])]

    # Find column indices for different data types
    column_indices = {}
    for i, header in enumerate(headers):
        header_lower = header.lower()
        if 'updated aosp versions' in header_lower or 'aosp versions' in header_lower:
            column_indices['versions'] = i
        elif 'cve' in header_lower:
            column_indices['cve'] = i
        elif 'references' in header_lower:
            column_indices['references'] = i
        elif 'type' in header_lower:
            column_indices['type'] = i
        elif 'severity' in header_lower:
            column_indices['severity'] = i

    # Process each data row
    for row in table.find_all('tr')[1:]:  # Skip header row
        cells = row.find_all(['td', 'th'])
        if len(cells) < len(headers):
            continue

        # Extract row-level information
        row_info = {
            'cve': None,
            'references': [],
            'type': None,
            'severity': None,
            'android_versions': []
        }

        # Extract Android versions
        if 'versions' in column_indices and column_indices['versions'] < len(cells):
            versions_cell = cells[column_indices['versions']]
            versions_text = versions_cell.get_text(strip=True)

            # Parse comma-separated versions like "15, 16" or "13, 14, 15"
            if versions_text:
                # Split by comma and clean up each version
                version_parts = [part.strip() for part in versions_text.split(',')]
                for part in version_parts:
                    # Remove any non-digit characters except for '+'
                    clean_part = part.strip()
                    if clean_part.replace('+', '').isdigit():
                        row_info['android_versions'].append(clean_part)

        # Extract CVE
        if 'cve' in column_indices and column_indices['cve'] < len(cells):
            cve_cell = cells[column_indices['cve']]
            cve_text = cve_cell.get_text(strip=True)
            if cve_text and cve_text.startswith('CVE-'):
                row_info['cve'] = cve_text

        # Extract references
        if 'references' in column_indices and column_indices['references'] < len(cells):
            ref_cell = cells[column_indices['references']]
            ref_text = ref_cell.get_text(strip=True)
            if ref_text:
                # Split by comma if multiple references
                refs = [ref.strip() for ref in ref_text.split(',') if ref.strip()]
                # Clean up footnote markers like [1], [2], [3] etc.
                cleaned_refs = []
                for ref in refs:
                    # Remove footnote markers at the end: [1], [2], etc.
                    cleaned_ref = re.sub(r'\s*\[\d+\]\s*$', '', ref).strip()
                    if cleaned_ref:
                        cleaned_refs.append(cleaned_ref)
                row_info['references'].extend(cleaned_refs)

        # Extract type
        if 'type' in column_indices and column_indices['type'] < len(cells):
            type_cell = cells[column_indices['type']]
            type_text = type_cell.get_text(strip=True)
            if type_text:
                row_info['type'] = type_text

        # Extract severity
        if 'severity' in column_indices and column_indices['severity'] < len(cells):
            severity_cell = cells[column_indices['severity']]
            severity_text = severity_cell.get_text(strip=True)
            if severity_text:
                row_info['severity'] = severity_text

        # Look for Android source links in all cells of this row
        found_patches = []
        for cell in cells:
            for link in cell.find_all('a', href=True):
                href = link.get('href')

                # Handle relative URLs
                if href.startswith('/'):
                    href = f"https://source.android.com{href}"
                elif href.startswith('//'):
                    href = f"https:{href}"

                # Check for Android source links
                if 'android.googlesource.com' in href and '/+/' in href:
                    patch_info = parse_git_url(href, row_info['android_versions'] if row_info['android_versions'] else None)
                    if patch_info:
                        # Add row-level information to patch
                        patch_info['component'] = current_component or 'System'
                        patch_info['cve'] = row_info['cve']
                        patch_info['references'] = row_info['references']
                        patch_info['type'] = row_info['type']
                        patch_info['severity'] = row_info['severity']

                        # Ensure android_versions is always present, set to null if empty
                        if 'android_versions' not in patch_info:
                            patch_info['android_versions'] = None

                        found_patches.append(patch_info)

        # Add patches, avoiding duplicates
        for patch_info in found_patches:
            is_duplicate = False
            for existing_patch in patches:
                if (existing_patch['ref'] == patch_info['ref'] and
                    existing_patch['name'] == patch_info['name']):
                    is_duplicate = True
                    break

            if not is_duplicate:
                patches.append(patch_info)

    return patches


def fetch_bulletin(date_str):
    """Fetch bulletin HTML for given date (format: YYYY-MM-DD)."""
    url = f"https://source.android.com/docs/security/bulletin/{date_str}"

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        print(f"Error fetching bulletin: {e}", file=sys.stderr)
        sys.exit(1)


def extract_patches(html):
    """Extract patch information from bulletin HTML."""
    soup = BeautifulSoup(html, 'html.parser')
    result = []

    # Find all security patch level sections
    patch_level_pattern = re.compile(r'(\d{4}-\d{2}-\d{2})\s+security patch level', re.IGNORECASE)

    # Look for headings that contain security patch levels
    for heading in soup.find_all(['h2', 'h3', 'h4']):
        heading_text = heading.get_text(strip=True)
        match = patch_level_pattern.search(heading_text)

        if match:
            patch_level = match.group(1)
            patches = []
            current_component = None

            # Find content after this heading until next patch level or end
            current = heading.find_next_sibling()

            while current:
                # Check if we hit another patch level heading
                if current.name in ['h2', 'h3', 'h4']:
                    next_heading_text = current.get_text(strip=True)
                    if patch_level_pattern.search(next_heading_text):
                        break
                    # This could be a component name
                    if current.name == 'h3' or current.name == 'h4':
                        current_component = next_heading_text

                # Look for tables containing vulnerability information
                elif current.name == 'table':
                    table_patches = parse_vulnerability_table(current, current_component)
                    patches.extend(table_patches)

                current = current.find_next_sibling()

            if patches:
                result.append({
                    "security_patch_level": patch_level,
                    "patches": patches
                })

    return result


def get_bulletin_patches(date_str):
    """Get bulletin patches for a given date (YYYY-MM-DD format) - for importing."""
    if not re.match(r'\d{4}-\d{2}-\d{2}', date_str):
        raise ValueError("Date must be in YYYY-MM-DD format")

    html = fetch_bulletin(date_str)
    patches = extract_patches(html)
    return patches


def main():
    if len(sys.argv) != 2:
        print("Usage: python fetch_bulletin.py YYYY-MM-DD", file=sys.stderr)
        print("Example: python fetch_bulletin.py 2025-09-01", file=sys.stderr)
        sys.exit(1)

    date_str = sys.argv[1]

    # Validate date format
    if not re.match(r'\d{4}-\d{2}-\d{2}', date_str):
        print("Error: Date must be in YYYY-MM-DD format", file=sys.stderr)
        sys.exit(1)

    # Fetch and parse
    patches = get_bulletin_patches(date_str)

    # Output JSON
    print(json.dumps(patches, indent=2))


if __name__ == "__main__":
    main()
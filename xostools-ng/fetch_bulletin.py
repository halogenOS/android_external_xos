#!/usr/bin/env python3
"""
Android Security Bulletin fetcher that extracts patch information as JSON.
Usage: python fetch_bulletin.py 2025-09-01
"""

import sys
import json
import re
import os
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
import git
import tempfile
from pathlib import Path
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn
from xos_common import get_android_top

console = Console()

# Debug flag - set to True to enable debug output
DEBUG = os.environ.get('DEBUG', 'False').lower() in ('true', '1', 'yes')

def debug_print(message):
    """Print debug message if DEBUG is enabled."""
    if DEBUG:
        console.print(message)


def extract_cherry_picked_commit(line):
    """Extract commit hash from cherry-picked line."""
    match = re.search(r'commit:([a-f0-9]{40})', line)
    return match.group(1) if match else None


def parse_metadata_line(line):
    """Parse a metadata line in Key: Value format."""
    if ':' not in line or line.rstrip().endswith(':'):
        return None, None
    
    key, value = line.split(':', 1)
    key = key.strip()
    value = value.strip()
    
    # Skip if no whitespace after colon or whitespace before colon
    if not value or key != key.rstrip():
        return None, None
    
    return key, value


def collect_multiline_value(lines, start_index, initial_value):
    """Collect multiline value from continuation lines."""
    full_value = initial_value
    i = start_index + 1
    
    while i < len(lines):
        next_line = lines[i]
        # If next line doesn't start at column 0 and doesn't contain ':', it's a continuation
        if next_line and not next_line[0].isalnum() and ':' not in next_line:
            full_value += '\n' + next_line.strip()
            i += 1
        elif next_line.strip() == '':
            # Skip empty lines but don't add to value
            i += 1
        else:
            # Next line starts a new key or is not a continuation
            break
    
    return full_value, i


def add_metadata_value(metadata, key, value):
    """Add metadata value, handling multiple values for the same key."""
    if key in metadata:
        if isinstance(metadata[key], list):
            metadata[key].append(value)
        else:
            metadata[key] = [metadata[key], value]
    else:
        # Special handling for Bug entries - always make them arrays for consistency
        if key == "Bug":
            metadata[key] = [value]
        else:
            metadata[key] = value


def parse_commit_message(commit_message):
    """Parse commit message to extract title and metadata."""
    lines = commit_message.strip().split('\n')

    if not lines:
        return "", {}, None

    title = lines[0].strip()
    metadata = {}
    cherry_picked_from = None

    i = 1
    while i < len(lines):
        line = lines[i].strip()

        # Skip empty lines
        if not line:
            i += 1
            continue

        # Handle cherry-picked lines specially
        if line.startswith('(cherry picked from'):
            cherry_picked_from = extract_cherry_picked_commit(line)
            i += 1
            continue

        # Look for metadata lines (Key: Value format)
        key, value = parse_metadata_line(line)
        if key and value:
            # Handle multiline values by collecting continuation lines
            full_value, next_index = collect_multiline_value(lines, i, value)
            add_metadata_value(metadata, key, full_value)
            i = next_index
        else:
            i += 1

    return title, metadata, cherry_picked_from


def extract_repo_name_from_url(repo_url):
    """Extract repository name from URL."""
    if '/platform/' not in repo_url:
        return None
    
    repo_name = repo_url.split('/platform/', 1)[1]
    if repo_name.endswith('.git'):
        repo_name = repo_name[:-4]
    return f"platform/{repo_name}"


def find_local_repo_path(android_top, repo_name):
    """Find local repository path from manifest."""
    default_xml = android_top / '.repo' / 'manifests' / 'default.xml'
    if not default_xml.exists():
        return None

    tree = ET.parse(default_xml)
    root = tree.getroot()

    # Find the project with matching name
    for project in root.findall('project'):
        project_name = project.get('name')
        if project_name == repo_name:
            return project.get('path', project_name)
    
    return None


def extract_base_repo_url(repo_url):
    """Extract base repository URL from commit URL."""
    if '/+/' in repo_url:
        # Google source format: https://domain/repo/+/commit
        base_url = repo_url.split('/+/')[0]
    elif '/-/commit/' in repo_url:
        # GitLab format: https://domain/repo/-/commit/commit
        base_url = repo_url.split('/-/commit/')[0]
    elif '/commit/' in repo_url:
        # GitHub format: https://domain/repo/commit/commit
        base_url = repo_url.split('/commit/')[0]
    else:
        raise ValueError(f"Unknown URL format: {repo_url}")

    # Add .git if not present
    if not base_url.endswith('.git'):
        base_url += '.git'
    
    return base_url


def fetch_commit_from_remote(repo, repo_url, commit_ref, repo_name, progress, commit_task):
    """Fetch commit from remote repository if not found locally."""
    try:
        base_url = extract_base_repo_url(repo_url)
        repo.git.fetch(base_url, commit_ref)
        commit = repo.commit(commit_ref)
        if progress and commit_task:
            progress.update(commit_task, advance=1.0)
        return commit
    except git.exc.GitCommandError as e:
        error_msg = str(e).split('\n')[0] if '\n' in str(e) else str(e)
        console.print(f"  [red]✗[/red] {repo_name}:{commit_ref[:8]} ([red]{error_msg}[/red])")
        debug_print(f"[red]DEBUG: Failed to fetch commit {commit_ref[:8]}: {e}[/red]")
        if progress and commit_task:
            progress.update(commit_task, advance=1.0)
        return None


def get_commit_from_repo(repo, commit_ref, repo_url, repo_name, progress, commit_task):
    """Get commit from repository, fetching from remote if necessary."""
    # Check if commit exists locally first
    try:
        commit = repo.commit(commit_ref)
        if progress and commit_task:
            progress.update(commit_task, advance=1.0)
        return commit
    except (git.exc.BadName, Exception):
        # Commit not found locally, fetch it from the original URL
        return fetch_commit_from_remote(repo, repo_url, commit_ref, repo_name, progress, commit_task)


def fetch_commit_info(repo_url, commit_ref, progress_task=None, progress=None):
    """Fetch commit information from existing repository in Android source tree."""
    debug_print(f"[magenta]DEBUG: fetch_commit_info called with repo_url={repo_url} commit_ref={commit_ref}[/magenta]")
    
    try:
        # Get Android source root
        android_top = get_android_top()
        debug_print(f"[magenta]DEBUG: android_top={android_top}[/magenta]")

        # Extract repository name from URL
        repo_name = extract_repo_name_from_url(repo_url)
        if not repo_name:
            debug_print(f"[red]DEBUG: Could not extract repo_name from URL: {repo_url}[/red]")
            return None

        debug_print(f"[blue]DEBUG: Extracted repo_name: {repo_name}[/blue]")

        commit_task = None
        if progress and progress_task:
            # Add a sub-task for commit fetching
            commit_task = progress.add_task(f"[yellow]├─ Fetching {repo_name}:{commit_ref[:8]}", total=1)

        # Find the local path
        local_path = find_local_repo_path(android_top, repo_name)
        if not local_path:
            debug_print(f"[red]DEBUG: Could not find local_path for repo_name: {repo_name}[/red]")
            if commit_task:
                progress.remove_task(commit_task)
            return None

        debug_print(f"[blue]DEBUG: Found local_path: {local_path}[/blue]")

        repo_full_path = android_top / local_path

        # Check if the repository exists
        if not repo_full_path.exists() or not (repo_full_path / '.git').exists():
            if commit_task:
                progress.remove_task(commit_task)
            return None

        try:
            # Open existing repository
            repo = git.Repo(repo_full_path)

            # Get commit (locally or from remote)
            commit = get_commit_from_repo(repo, commit_ref, repo_url, repo_name, progress, commit_task)
            if not commit:
                if commit_task:
                    progress.remove_task(commit_task)
                return None

            title, metadata, cherry_picked_from = parse_commit_message(commit.message)

            if commit_task:
                progress.remove_task(commit_task)

            result = {
                "title": title,
                "metadata": metadata
            }

            if cherry_picked_from:
                result["cherry_picked_from"] = cherry_picked_from

            return result

        except Exception as e:
            debug_print(f"[red]DEBUG: Inner exception in fetch_commit_info: {e}[/red]")
            if commit_task:
                progress.remove_task(commit_task)
            return None

    except Exception as e:
        debug_print(f"[red]DEBUG: Exception in fetch_commit_info: {e}[/red]")
        return None


def parse_git_url(url, android_versions=None, progress_task=None, progress=None):
    """Parse git URL to extract repo and commit info."""
    # Handle different git URL formats:
    # Google: https://android.googlesource.com/platform/frameworks/base/+/ed39b7c3895c8c63a1ccdbcc9783a2d3ca15127f
    # GitLab: https://git.codelinaro.org/clo/la/platform/vendor/qcom-opensource/dpm-commonsys/-/commit/6091318f8b78e24d019aa3533795891d3f16d80d
    # GitHub: https://github.com/owner/repo/commit/abcd1234

    patterns = [
        # Google format: /+/commit_hash
        r'(https://[^/]+/[^/]+(?:/[^/]+)*)/\+/([a-f0-9]+)',
        # GitLab format: /-/commit/commit_hash
        r'(https://[^/]+/[^/]+(?:/[^/]+)*)/-/commit/([a-f0-9]+)',
        # GitHub format: /commit/commit_hash
        r'(https://[^/]+/[^/]+/[^/]+)/commit/([a-f0-9]+)'
    ]

    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            repo_url = match.group(1)
            commit_ref = match.group(2)

            # Extract repo name from the repo URL path
            repo_path = repo_url.split('/', 3)[-1] if repo_url.count('/') >= 3 else repo_url

            patch_info = {
                "url": url,
                "repo": repo_url,
                "remote": "aosp",
                "name": repo_path,
                "ref": commit_ref
            }

            if android_versions:
                patch_info["android_versions"] = android_versions

            # Fetch commit information
            debug_print(f"[blue]DEBUG: About to call fetch_commit_info for {patch_info['repo']} {patch_info['ref'][:8]}[/blue]")
            commit_info = fetch_commit_info(patch_info["repo"], patch_info["ref"], progress_task, progress)
            debug_print(f"[blue]DEBUG: fetch_commit_info returned: {commit_info}[/blue]")
            if commit_info:
                patch_info["title"] = commit_info["title"]
                patch_info["metadata"] = commit_info["metadata"]
                if "cherry_picked_from" in commit_info:
                    patch_info["cherry_picked_from"] = commit_info["cherry_picked_from"]
            else:
                debug_print(f"[red]DEBUG: No commit info returned for {patch_info['ref'][:8]}[/red]")

            return patch_info

    return None


def find_table_column_indices(headers):
    """Find column indices for different data types in table headers."""
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
    return column_indices


def parse_android_versions(versions_text):
    """Parse Android versions from text like '15, 16' or '13, 14, 15'."""
    android_versions = []
    if versions_text:
        # Split by comma and clean up each version
        version_parts = [part.strip() for part in versions_text.split(',')]
        for part in version_parts:
            # Remove any non-digit characters except for '+'
            clean_part = part.strip()
            if clean_part.replace('+', '').isdigit():
                android_versions.append(clean_part)
    return android_versions


def clean_reference_text(ref_text):
    """Clean reference text by removing footnote markers."""
    if not ref_text:
        return []
    
    # Split by comma if multiple references
    refs = [ref.strip() for ref in ref_text.split(',') if ref.strip()]
    # Clean up footnote markers like [1], [2], [3] etc.
    cleaned_refs = []
    for ref in refs:
        # Remove footnote markers at the end: [1], [2], etc.
        cleaned_ref = re.sub(r'\s*\[\d+\]\s*$', '', ref).strip()
        if cleaned_ref:
            cleaned_refs.append(cleaned_ref)
    return cleaned_refs


def extract_row_information(cells, column_indices):
    """Extract row-level information from table cells."""
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
        row_info['android_versions'] = parse_android_versions(versions_text)

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
        row_info['references'] = clean_reference_text(ref_text)

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

    return row_info


def collect_git_links_from_row(cells):
    """Collect all git links from table row cells."""
    git_links = []
    
    for cell in cells:
        for link in cell.find_all('a', href=True):
            href = link.get('href')

            # Handle relative URLs
            if href.startswith('/'):
                href = f"https://source.android.com{href}"
            elif href.startswith('//'):
                href = f"https:{href}"

            # Check for git source links with commit references
            if '/+/' in href or '/-/commit/' in href or '/commit/' in href:
                git_links.append(href)
    
    return git_links


def create_patch_info(patch_info, row_info, current_component):
    """Create complete patch info by adding row-level information."""
    patch_info['component'] = current_component or 'System'
    patch_info['cve'] = row_info['cve']
    patch_info['references'] = row_info['references']
    patch_info['type'] = row_info['type']
    patch_info['severity'] = row_info['severity']

    # Ensure android_versions is always present, set to null if empty
    if 'android_versions' not in patch_info:
        patch_info['android_versions'] = None
    
    return patch_info


def is_duplicate_patch(patch_info, existing_patches):
    """Check if patch is a duplicate of existing patches."""
    for existing_patch in existing_patches:
        if (existing_patch['ref'] == patch_info['ref'] and
            existing_patch['name'] == patch_info['name']):
            return True
    return False


def parse_vulnerability_table(table, current_component, progress_task=None, progress=None):
    """Parse a vulnerability table to extract patches with Android version information."""
    patches = []

    # Find header row
    header_row = table.find('tr')
    if not header_row:
        return patches

    headers = [cell.get_text(strip=True) for cell in header_row.find_all(['th', 'td'])]
    column_indices = find_table_column_indices(headers)

    # Process each data row
    for row in table.find_all('tr')[1:]:  # Skip header row
        cells = row.find_all(['td', 'th'])
        if len(cells) < len(headers):
            continue

        # Extract row-level information
        row_info = extract_row_information(cells, column_indices)

        # Collect all git links from this row
        git_links = collect_git_links_from_row(cells)

        # Process git links to create patch info
        for href in git_links:
            patch_info = parse_git_url(href, row_info['android_versions'] if row_info['android_versions'] else None, progress_task, progress)
            if patch_info:
                # Add row-level information to patch
                patch_info = create_patch_info(patch_info, row_info, current_component)

                # Add patches, avoiding duplicates
                if not is_duplicate_patch(patch_info, patches):
                    patches.append(patch_info)

    return patches


def fetch_bulletin(date_str):
    """Fetch bulletin HTML for given date (format: YYYY-MM-DD)."""
    url = f"https://source.android.com/docs/security/bulletin/{date_str}"

    console.print(f"[cyan]Fetching bulletin from {url}...[/cyan]")
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        console.print(f"[green]Successfully fetched bulletin HTML ({len(response.text):,} bytes)[/green]")
        return response.text
    except requests.RequestException as e:
        console.print(f"[red]Error fetching bulletin: {e}[/red]")
        sys.exit(1)


def find_patch_level_headings(soup):
    """Find all security patch level headings in the HTML."""
    patch_level_pattern = re.compile(r'(\d{4}-\d{2}-\d{2})\s+security patch level', re.IGNORECASE)
    patch_level_headings = []
    
    for heading in soup.find_all(['h2', 'h3', 'h4']):
        heading_text = heading.get_text(strip=True)
        match = patch_level_pattern.search(heading_text)
        if match:
            patch_level_headings.append((heading, match.group(1)))
    
    return patch_level_headings


def process_patch_level_content(heading, patch_level, patch_level_task, progress):
    """Process content for a specific patch level."""
    patch_level_pattern = re.compile(r'(\d{4}-\d{2}-\d{2})\s+security patch level', re.IGNORECASE)
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
            table_patches = parse_vulnerability_table(current, current_component, patch_level_task, progress)
            patches.extend(table_patches)

        current = current.find_next_sibling()
    
    return patches


def extract_patches(html):
    """Extract patch information from bulletin HTML."""
    soup = BeautifulSoup(html, 'html.parser')
    result = []

    console.print("[cyan]Parsing bulletin HTML...[/cyan]")

    # Collect all patch level headings first
    patch_level_headings = find_patch_level_headings(soup)
    console.print(f"[green]Found {len(patch_level_headings)} security patch levels[/green]")

    # Process each patch level with progress
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console
    ) as progress:

        patch_level_task = progress.add_task(
            "[cyan]Processing patch levels",
            total=len(patch_level_headings)
        )

        for heading, patch_level in patch_level_headings:
            progress.update(patch_level_task, description=f"[cyan]Processing {patch_level}")

            patches = process_patch_level_content(heading, patch_level, patch_level_task, progress)

            if patches:
                result.append({
                    "security_patch_level": patch_level,
                    "patches": patches
                })
                progress.update(patch_level_task, description=f"[green]{patch_level}: {len(patches)} patches extracted")
            else:
                progress.update(patch_level_task, description=f"[yellow]{patch_level}: No applicable patches")

            progress.update(patch_level_task, advance=1)

    total_patches = sum(len(pl.get('patches', [])) for pl in result)
    console.print(f"[green]Extracted {total_patches} patches total across {len(result)} patch levels[/green]")
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
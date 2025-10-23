#!/usr/bin/env -S uv run
# /// script
# dependencies = [
#   "playwright",
#   "requests",
#   "python-dotenv",
#   "pillow",
#   "tqdm",
#   "aiohttp",
# ]
# ///

"""
Demo Gallery Generator
Usage: uv run screenshotter.py
"""

import asyncio
import os
import re
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import requests
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright
import sys
from urllib.parse import urlparse
from tqdm import tqdm

def extract_github_username(url):
    """
    Extract GitHub username from a GitHub URL.
    
    Args:
        url (str): GitHub URL (repo or pages)
        
    Returns:
        str: GitHub username or 'Unknown' if extraction fails
    """
    if not url:
        return 'Unknown'
    
    try:
        # Handle both github.com and github.io URLs
        if 'github.io' in url:
            # Extract from pages URL like https://username.github.io/repo
            parsed = urlparse(url)
            hostname = parsed.netloc.lower()
            if hostname.endswith('.github.io'):
                return hostname.replace('.github.io', '')
        elif 'github.com' in url:
            # Extract from repo URL like https://github.com/username/repo
            parsed = urlparse(url)
            path_parts = [part for part in parsed.path.split('/') if part]
            if len(path_parts) >= 1:
                return path_parts[0]
    except Exception:
        pass
    
    return 'Unknown'


def github_repo_to_pages_url(repo_url):
    """Simple GitHub repo to pages URL conversion"""
    if not repo_url or 'github.com' not in repo_url:
        return repo_url
    # Convert https://github.com/user/repo to https://user.github.io/repo
    parts = repo_url.replace('https://github.com/', '').split('/')
    if len(parts) >= 2:
        user, repo = parts[0], parts[1]
        return f"https://{user}.github.io/{repo}"
    return repo_url





def process_submission_url(submission_url):
    """Simple URL processing - convert GitHub repo URLs to pages URLs"""
    if not submission_url:
        return submission_url, False, None
    
    # Convert GitHub repo URLs to pages URLs
    if 'github.com' in submission_url and '.github.io' not in submission_url:
        return github_repo_to_pages_url(submission_url), True, None
    
    return submission_url, False, None

def ensure_playwright_browsers():
    """Ensure Playwright browsers are installed"""
    try:
        # Check if browsers are installed
        with sync_playwright() as p:
            p.chromium.launch()
    except Exception:
        print("📥 Installing Playwright browsers (first time only)...")
        os.system(f"{sys.executable} -m playwright install chromium")

# Load environment variables
load_dotenv()

CANVAS_API_TOKEN = os.getenv("CANVAS_API_TOKEN")
CANVAS_BASE_URL = os.getenv("CANVAS_BASE_URL")  # e.g., "https://canvas.university.edu"
COURSE_ID = os.getenv("COURSE_ID")
ASSIGNMENT_ID = os.getenv("ASSIGNMENT_ID")

# Concurrency configuration
MAX_CONCURRENT_FETCHES = 16
MAX_CONCURRENT_SCREENSHOTS = 16

OUTPUT_DIR = Path("docs")
SCREENSHOTS_DIR = OUTPUT_DIR / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def parse_link_header(link_header):
    """Parse RFC 5988 Link header to extract pagination URLs"""
    if not link_header:
        return {}
    
    links = {}
    # Split by comma to get individual links
    for link in link_header.split(','):
        link = link.strip()
        # Use regex to extract URL and rel
        match = re.match(r'<([^>]+)>;\s*rel="([^"]+)"', link)
        if match:
            url, rel = match.groups()
            links[rel] = url
    
    return links


async def fetch_submissions():
    """Fetch all submissions from Canvas LMS with pagination support using aiohttp"""
    import aiohttp
    
    headers = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}
    
    url = f"{CANVAS_BASE_URL}/api/v1/courses/{COURSE_ID}/assignments/{ASSIGNMENT_ID}/submissions"
    params = {"per_page": 100}
    
    all_submissions = []
    page_count = 0
    
    async with aiohttp.ClientSession(headers=headers) as session:
        while url:
            page_count += 1
            print(f"Fetching submissions page {page_count}...")
            
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                page_submissions = await response.json()
                all_submissions.extend(page_submissions)
                
                # Parse Link header for next page
                link_header = response.headers.get('Link')
                links = parse_link_header(link_header)
                
                # Get next page URL, or None if no more pages
                url = links.get('next')
                # Clear params for subsequent requests as they're included in the URL
                params = None
    
    print(f"Retrieved {len(all_submissions)} total submissions from {page_count} pages")
    submissions = all_submissions
    
    # Extract student info and URLs
    valid_submissions = [sub for sub in submissions if sub.get("workflow_state") in ["submitted", "graded"] and sub.get("url")]
    print(f"Processing {len(valid_submissions)} valid submissions...")
    
    # Fetch all user details concurrently
    projects = await fetch_user_details_concurrent(valid_submissions)
    
    return projects


async def fetch_user_details_concurrent(submissions):
    """Fetch user details for all submissions concurrently using aiohttp"""
    import aiohttp
    
    headers = {"Authorization": f"Bearer {CANVAS_API_TOKEN}"}
    projects = []
    
    # Semaphore to limit concurrent requests to Canvas API
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
    
    async def fetch_user_detail(session, sub):
        async with semaphore:
            try:
                user_id = sub["user_id"]
                submission_id = sub["id"]  # Use submission ID instead of student ID for privacy
                user_url = f"{CANVAS_BASE_URL}/api/v1/courses/{COURSE_ID}/users/{user_id}"
                
                async with session.get(user_url) as response:
                    response.raise_for_status()
                    user_data = await response.json()
                    
                    # Get the original submitted URL
                    original_url = sub.get("url") or sub.get("submission_comments", [{}])[0].get("comment", "")
                    
                    # Process the URL to convert GitHub repo URLs to GitHub Pages URLs
                    processed_url, was_converted, error_msg = process_submission_url(original_url)
                    
                    # Extract GitHub username for display (FERPA compliance)
                    github_username = extract_github_username(original_url)
                    
                    project_data = {
                        "student_name": user_data.get("name", "Unknown"),  # Keep for internal use
                        "submission_id": submission_id,  # Use submission ID for privacy
                        "original_url": original_url,
                        "url": processed_url,
                        "was_converted": was_converted,
                        "github_username": github_username
                    }
                    
                    # Add error information if URL conversion failed
                    if error_msg:
                        project_data["url_error"] = error_msg
                        print(f"⚠️  URL processing warning for {user_data.get('name', 'Unknown')}: {error_msg}")
                    
                    return project_data
                    
            except Exception as e:
                print(f"Error fetching user {sub['user_id']}: {e}")
                return None
    
    async with aiohttp.ClientSession(headers=headers) as session:
        # Create tasks for all user detail fetches
        tasks = [fetch_user_detail(session, sub) for sub in submissions]
        
        # Use tqdm for progress tracking
        with tqdm(total=len(tasks), desc="Fetching user details", unit="user") as pbar:
            # Process tasks as they complete
            for task in asyncio.as_completed(tasks):
                result = await task
                if result:
                    projects.append(result)
                pbar.update(1)
    
    return projects


async def capture_demo_screenshot(page, url, output_prefix):
    """Capture a single screenshot of a demo with no interaction"""
    console_messages = []
    error_messages = []
    
    # Set up console message capture
    def handle_console(msg):
        console_messages.append(f"[{msg.type}] {msg.text}")
    
    def handle_page_error(error):
        error_messages.append(f"Page error: {error}")
    
    page.on("console", handle_console)
    page.on("pageerror", handle_page_error)
    
    try:
        # Navigate to the demo
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)  # Let the demo initialize
        
        # Take single screenshot
        screenshot_path = f"{output_prefix}_0.png"
        await page.screenshot(path=screenshot_path, full_page=False)
        
        # Combine all captured messages
        all_messages = console_messages + error_messages
        
        return screenshot_path, all_messages
    
    except Exception as e:
        error_msg = f"Screenshot error: {str(e)}"
        error_messages.append(error_msg)
        print(f"Error capturing {url}: {e}")
        
        # Create a placeholder image
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new('RGB', (800, 600), color='#1a1a1a')
        d = ImageDraw.Draw(img)
        d.text((400, 300), "Failed to load", fill='white', anchor="mm")
        placeholder = f"{output_prefix}_0.png"
        img.save(placeholder)
        
        all_messages = console_messages + error_messages
        return placeholder, all_messages



async def process_single_project_screenshot(browser, project, semaphore):
    """Process a single project with semaphore-controlled concurrency"""
    async with semaphore:
        context = await browser.new_context(viewport={"width": 800, "height": 600})
        page = await context.new_page()
        
        try:
            output_prefix = SCREENSHOTS_DIR / f"demo_{project['submission_id']}"
            screenshot_path, console_messages = await capture_demo_screenshot(
                page, 
                project['url'], 
                str(output_prefix)
            )
            
            # Extract GitHub username from URL
            github_username = extract_github_username(project.get('original_url') or project.get('url'))
            
            result = {
                **project,
                "screenshot": str(Path(screenshot_path).relative_to(OUTPUT_DIR)),
                "console_messages": console_messages,
                "github_username": github_username
            }
            
            # Be nice to servers
            await asyncio.sleep(1)
            
            return result
            
        finally:
            await context.close()


async def process_all_projects_screenshots(projects):
    """Process all projects and capture screenshots with controlled concurrency"""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_SCREENSHOTS)
        
        results = []
        
        # Create tasks for all projects
        tasks = [process_single_project_screenshot(browser, project, semaphore) for project in projects]
        
        # Use tqdm for progress tracking
        progress_bar = tqdm(total=len(projects), desc="Capturing screenshots", unit="project")
        
        # Process tasks as they complete
        for task in asyncio.as_completed(tasks):
            result = await task
            results.append(result)
            
            # Update progress bar
            progress_bar.update(1)
            
            # Update HTML page after each screenshot
            update_html_with_progress(results, projects)
        
        progress_bar.close()
        await browser.close()
        
    return results


def update_html_with_progress(completed_projects, all_projects):
    """Update HTML page with current progress"""
    html = generate_html_content(completed_projects, all_projects)
    output_file = OUTPUT_DIR / "index.html"
    output_file.write_text(html)


def generate_html(projects):
    """Generate the gallery HTML page"""
    return generate_html_content(projects, projects)


def generate_html_content(completed_projects, all_projects):
    """Generate the gallery HTML page content showing all projects with placeholders"""
    
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Demo Gallery</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f0f0f;
            color: #fff;
            padding: 2rem;
        }
        
        h1 {
            text-align: center;
            margin-bottom: 3rem;
            font-size: 2.5rem;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .gallery {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
            gap: 2rem;
            max-width: 1400px;
            margin: 0 auto;
        }
        
        .project-card {
            background: #1a1a1a;
            border-radius: 12px;
            overflow: hidden;
            transition: transform 0.3s ease, box-shadow 0.3s ease;
            border: 1px solid #333;
        }
        
        .project-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 40px rgba(102, 126, 234, 0.3);
        }
        
        .screenshot-container {
            position: relative;
            width: 100%;
            height: 300px;
            overflow: hidden;
            background: #000;
        }
        
        .screenshot-container img {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }
        
        .project-info {
            padding: 1.5rem;
        }
        
        .project-info h2 {
            font-size: 1.5rem;
            margin-bottom: 0.5rem;
        }
        
        .project-info a {
            color: #667eea;
            text-decoration: none;
            display: inline-block;
            margin-top: 0.5rem;
            margin-right: 1rem;
        }
        
        .project-info a:hover {
            text-decoration: underline;
        }
        
        .links {
            margin-top: 0.5rem;
        }
        
        .submitted-date {
            color: #888;
            font-size: 0.9rem;
            margin-top: 0.5rem;
        }
        
        .update-info {
            text-align: center;
            color: #666;
            margin-top: 3rem;
            font-size: 0.9rem;
        }
        

    </style>
</head>
<body>
    <h1>Demo Gallery</h1>
    
    <div class="gallery">
"""
    
    # Create a lookup of completed projects by submission_id
    completed_lookup = {proj['submission_id']: proj for proj in completed_projects}

    for project in all_projects:
        # Check if this project has been completed
        completed_project = completed_lookup.get(project['submission_id'])
        
        if completed_project:
            # Use completed project data
            screenshot = completed_project.get('screenshot') or (completed_project.get('screenshots', [None])[0])
            screenshot_html = f'<img src="{screenshot}" alt="Demo Screenshot">' if screenshot else '<div style="display: flex; align-items: center; justify-content: center; height: 100%; color: #666;">Failed to load</div>'
            
            # Add console messages if any
            console_messages = completed_project.get('console_messages', [])
            console_html = ""
            if console_messages:
                messages_text = "\n".join(console_messages)
                console_html = f'''
                <details style="margin-top: 0.5rem;">
                    <summary style="cursor: pointer; color: #888; font-size: 0.8rem;">Console Messages ({len(console_messages)})</summary>
                    <pre style="background: #000; color: #0f0; padding: 0.5rem; font-size: 0.7rem; max-height: 200px; overflow-y: auto; white-space: pre-wrap;">{messages_text}</pre>
                </details>'''
        else:
            # Show placeholder for not yet processed
            screenshot_html = '<div style="display: flex; align-items: center; justify-content: center; height: 100%; color: #666; font-style: italic;">Processing...</div>'
            console_html = ""
        
        # Use GitHub username instead of student name for FERPA compliance
        display_name = project.get('github_username', 'Unknown')
        
        # Determine the GitHub repo URL for browsing code
        original_url = project.get('original_url', '')
        if 'github.com' in original_url:
            code_url = original_url
        elif 'github.io' in original_url:
            # Convert github.io URL back to github.com repo URL
            # e.g., https://username.github.io/repo -> https://github.com/username/repo
            try:
                from urllib.parse import urlparse
                parsed = urlparse(original_url)
                hostname = parsed.netloc.lower()
                if hostname.endswith('.github.io'):
                    username = hostname.replace('.github.io', '')
                    path_parts = [part for part in parsed.path.split('/') if part]
                    if path_parts:
                        repo_name = path_parts[0]
                        code_url = f"https://github.com/{username}/{repo_name}"
                    else:
                        code_url = original_url
                else:
                    code_url = original_url
            except:
                code_url = original_url
        else:
            code_url = original_url
        
        html += f"""
        <div class="project-card">
            <div class="screenshot-container">
                {screenshot_html}
            </div>
            <div class="project-info">
                <h2>{display_name}</h2>
                <div class="links">
                    <a href="{project['url']}" target="_blank">View Demo →</a>
                    <a href="{code_url}" target="_blank">Browse Code →</a>
                </div>
                {console_html}
            </div>
        </div>
"""
    
    html += """
    </div>
    
    <div class="update-info">
        Last updated: """ + datetime.now().strftime('%B %d, %Y at %I:%M %p') + """
    </div>
</body>
</html>
"""
    
    return html


async def main():
    ensure_playwright_browsers()

    print("� Demo Gallery Generator")
    print("=" * 50)
    
    # Fetch submissions from Canvas
    print("\n📚 Fetching submissions from Canvas...")
    projects = await fetch_submissions()
    print(f"Found {len(projects)} submissions")
    
    # Skip URL verification for simplicity
    
    # Create initial HTML page
    output_file = OUTPUT_DIR / "index.html"
    print(f"\n🌐 Creating initial HTML page at {output_file}")
    initial_html = generate_html_content([], projects)
    output_file.write_text(initial_html)
    print(f"📄 You can now open {output_file} in your browser and refresh to see progress")
    
    # Capture screenshots with continuous HTML updates
    print(f"\n📸 Capturing screenshots with {MAX_CONCURRENT_SCREENSHOTS} concurrent jobs...")
    print("📄 HTML will update continuously as screenshots complete")
    results = await process_all_projects_screenshots(projects)
    
    # Generate final HTML
    print("\n🎨 Generating final gallery HTML...")
    final_html = generate_html(results)
    output_file.write_text(final_html)
    
    print(f"\n✅ Gallery generated successfully!")
    print(f"📁 Output directory: {OUTPUT_DIR}")
    print(f"🌐 View gallery at {output_file}")
    print(f"\n📤 Next steps:")
    print(f"   1. git add {OUTPUT_DIR}")
    print(f"   2. git commit -m 'Update gallery'")
    print(f"   3. git push")


if __name__ == "__main__":
    asyncio.run(main())
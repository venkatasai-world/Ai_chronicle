import os
import requests
import json
import feedparser
import random
import re
from pathlib import Path
from PIL import Image
from bing_image_downloader import downloader
from time import sleep
from flask import Flask, render_template, request, redirect, url_for, jsonify
import urllib.parse  # For URL-encoding the prompt
import edge_tts
import asyncio # edge-tts is async
from groq import Groq

# Initialize Flask app
app = Flask(__name__)

# Get PORT from environment variable (Render), default to 5000 for local
PORT = int(os.environ.get('PORT', 5000))

# Ensure directories exist
output_dir = Path("images")
output_dir.mkdir(parents=True, exist_ok=True)
os.makedirs('static/images', exist_ok=True)

# Track processed keywords to avoid repetition
processed_keywords = set()

rss_feeds = [
    {"name": "General News", "url": "https://feeds.feedburner.com/NDTV-LatestNews"},
    {"name": "International News", "url": "https://www.thehindu.com/news/international/feeder/default.rss"},
    {"name": "Entertainment", "url": "https://www.thehindu.com/entertainment/movies/feeder/default.rss"},
    {"name": "Education", "url": "https://feeds.bbci.co.uk/news/education/rss.xml"},
    {"name": "Sports", "url": "https://timesofindia.indiatimes.com/rssfeeds/913168846.cms"},
    {"name": "Science", "url": "https://moxie.foxnews.com/google-publisher/science.xml"},
    {"name": "Business", "url": "https://www.thehindu.com/business/Economy/feeder/default.rss"}
]

# Keep track of processed entry counts per feed URL
feed_entry_counts = {}


# =============================================================================
# New function: Use Groq API for text generation
# =============================================================================
def get_deepseek_response(prompt_text, max_retries=3):
    api_key = os.environ.get('GROQ_API_KEY')

    if not api_key:
        print("[ERROR] GROQ_API_KEY environment variable is not set.")
        return None

    try:
        client = Groq(api_key=api_key)
    except Exception as e:
        print(f"[ERROR] Failed to initialize Groq client: {e}")
        return None
    
    for attempt in range(max_retries):
        print(f"\n[SEND] Attempt {attempt+1}/{max_retries} - Sending request to Groq API...")
        try:
            message = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt_text}],
                model="llama3-70b-8192",
                timeout=30
            )
            response_text = message.choices[0].message.content
            print("[OK] Received response from Groq:")
            print(response_text[:200] + "...")
            return response_text
        except Exception as e:
            print(f"[EXCEPTION] Attempt {attempt+1} failed: {type(e).__name__}: {str(e)}")
            if attempt < max_retries - 1:
                sleep(2)
    
    print("[TIMEOUT] All retries exhausted. Returning None.")
    return None

# =============================================================================
# Image and Audio Processing Functions
# =============================================================================
def create_news_image(text, keyword, filename):
    """Create a background image (no baked-in text).

    The summary text is shown in the UI overlay (HTML). If we draw the text onto
    the JPG here too, it appears duplicated.
    """
    print(f"\n[IMAGE] Creating image for keyword: {keyword}")
    keyword = str(keyword).split(',')[0].strip()[:25]
    keyword = ''.join(c for c in keyword if c.isalnum() or c in (' ', '-', '_'))

    try:
        print(f"[SEARCH] Searching images for: {keyword}")
        downloader.download(
            f"{keyword}",
            limit=2,  # Download two images
            output_dir=str(output_dir),
            adult_filter_off=True,
            force_replace=False,
            timeout=30
        )
        img_dir = output_dir / keyword
        images = list(img_dir.glob('*'))
        if images:
            image_path = random.choice(images)
            print(f"[OK] Selected image at: {image_path}")
            img = Image.open(image_path).convert('RGB')
        else:
            raise FileNotFoundError("No images downloaded")
    except Exception as e:
        print(f"[FAILED] Image download failed: {str(e)}")
        print("[WARNING] Using fallback background")
        img = Image.new('RGB', (600, 1000), color=(255, 255, 255))

    canvas_width, canvas_height = 600, 1000
    canvas = Image.new('RGB', (canvas_width, canvas_height), color=(255, 255, 255))
    img_ratio = img.width / img.height
    new_height = 1000
    new_width = int(img_ratio * new_height)
    img = img.resize((new_width, new_height), Image.LANCZOS)

    if new_width > canvas_width:
        left = (new_width - canvas_width) // 2
        right = left + canvas_width
        img = img.crop((left, 0, right, new_height))
    else:
        paste_x = (canvas_width - new_width) // 2
        paste_y = 0
        canvas.paste(img, (paste_x, paste_y))
        img = canvas

    if img.width == canvas_width:
        canvas = img
    # Keep the image clean. The gallery overlay renders the summary text.
    if canvas.mode != "RGB":
        canvas = canvas.convert("RGB")

    canvas.save(f"static/images/{filename}", quality=92, optimize=True)
    print(f"[OK] Saved image: static/images/{filename}")

async def generate_speech(text, output_file):
    """Generates speech using edge-tts and saves it to a file."""
    try:
        voice = "en-US-JennyNeural"  # You can change the voice here, see edge-tts --list-voices for options
        communicate = edge_tts.Communicate(text, voice)
        with open(output_file, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
        print(f"[OK] Saved audio: {output_file}")
    except Exception as e:
        print(f"[FAILED] Audio generation failed: {e}")

def create_audio(text, filename):
    """Create an audio file from text using edge-tts"""
    output_file = f"static/images/{filename}.mp3"
    asyncio.run(generate_speech(text, output_file))


# =============================================================================
# RSS Processing Function
# =============================================================================
def process_rss_entries(rss_url, start_entry_index=0, num_entries=3):
    """Enhanced RSS processing with better validation and pagination"""
    print(f"\n[RSS] Processing RSS feed: {rss_url} from entry {start_entry_index}, count {num_entries}")
    generated_files = []
    processed_count = 0
    try:
        print("[FETCH] Fetching RSS entries...")
        feed = feedparser.parse(rss_url)
        if feed.bozo:
            print(f"[ERROR] RSS parsing error: {feed.bozo_exception}")
            return generated_files, processed_count

        entries = feed.entries
        total_entries = len(entries)
        print(f"[OK] Found {total_entries} total entries")

        if start_entry_index >= total_entries:
            print("[DONE] No more entries to process.")
            return generated_files, processed_count


        end_entry_index = min(start_entry_index + num_entries, total_entries)
        entries_to_process = entries[start_entry_index:end_entry_index]

        print(f"Processing entries {start_entry_index + 1} to {end_entry_index} ({len(entries_to_process)} entries)")


        file_index_start = start_entry_index + 1 # Filename index start
        for i, entry in enumerate(entries_to_process):
            current_file_index = file_index_start + i

            print(f"\n[ENTRY] Processing entry {current_file_index}/{total_entries}")
            title = entry.get('title', '').strip()
            description = entry.get('description', '').strip()

            if not title and not description:
                print("[SKIP] Skipping empty entry")
                continue

            print(f"[TITLE] Title: {title[:50]}...")
            print(f"[DESC] Description: {description[:50]}...")


            prompt = (
                "Generate valid JSON with exactly two keys:\n"
                "- \"text\": One short factual sentence about what happened. 18 words or fewer. "
                "Do NOT explain why, just what. No ellipses. No trailing dot at the end.\n"
                "- \"keyword\": A single term (maximum two words) related to the text, including a person's name if mentioned.\n\n"
                "News:\n"
                f"Title: {title}\n"
                f"Description: {description}\n\n"
                "JSON:"
            )

            output = get_deepseek_response(prompt)
            if not output:
                print("[WARNING] API returned None, using fallback data...")
                # Fallback: create summary from title/description
                result = {
                    'text': title[:100] if title else description[:100],
                    'keyword': title.split()[0] if title else 'News'
                }
            else:
                try:
                    print("[PARSE] Parsing response...")
                    json_start = output.find('{')
                    json_end = output.rfind('}') + 1
                    if json_start == -1 or json_end == 0:
                        print("[WARNING] No JSON found in response, using fallback...")
                        result = {
                            'text': title[:100] if title else description[:100],
                            'keyword': title.split()[0] if title else 'News'
                        }
                    else:
                        json_content = output[json_start:json_end]
                        result = json.loads(json_content)
                        print("[OK] Parsed JSON:", result)

                        if not all(k in result for k in ['text', 'keyword']):
                            raise ValueError("Missing required fields")
                except (json.JSONDecodeError, ValueError) as e:
                    print(f"[WARNING] JSON parsing failed ({e}), using fallback...")
                    result = {
                        'text': title[:100] if title else description[:100],
                        'keyword': title.split()[0] if title else 'News'
                    }

            # Normalise and clean summary text: short, factual, no extra dots or ellipses
            summary_text = str(result.get('text', '')).strip()
            if summary_text:
                # Remove ellipses anywhere
                summary_text = summary_text.replace('...', ' ')
                # Remove any trailing dots (one or many)
                summary_text = re.sub(r'\s*\.+\s*$', '', summary_text).strip()
            result['text'] = summary_text

            
            try:
                keyword = result['keyword']
                if keyword in processed_keywords:
                    print(f"[DUPLICATE] Keyword '{keyword}' already processed. Skipping to avoid repetition.")
                    continue

                processed_keywords.add(keyword)
                print(f"[SUMMARY] Summary: {result['text']}")
                print(f"[KEYWORD] Keyword: {keyword}")

                image_filename = f"news_summary_{current_file_index}.jpg"
                audio_filename = f"news_summary_{current_file_index}"

                create_news_image(result['text'], keyword, image_filename)
                create_audio(result['text'], audio_filename)

                # Store both filename and text for display in gallery
                generated_files.append({
                    'filename': image_filename,
                    'text': result['text']
                })
                processed_count += 1
            except Exception as e:
                print(f"[ERROR] Failed to generate image/audio: {str(e)}")
                continue

    except Exception as e:
        print(f"[ERROR] RSS processing failed: {str(e)}")

    return generated_files, processed_count

# =============================================================================
# Flask Routes
# =============================================================================
@app.route('/')
def index():
    return render_template('index.html', rss_feeds=rss_feeds)

@app.route('/process', methods=['POST'])
def process_feed():
    print("\n" + "="*80)
    print("[PROCESS] Starting feed processing...")
    selected_index = int(request.form['feed']) - 1
    selected_url = rss_feeds[selected_index]['url']
    print(f"[PROCESS] Selected feed index {selected_index}, URL: {selected_url}")
    
    processed_keywords.clear()
    feed_entry_counts[selected_url] = 0 # Initialize entry count

    # Clear previous images and audios
    print("[PROCESS] Clearing previous images...")
    cleared_count = 0
    for f in Path('static/images').glob('*.*'):
        print(f"[PROCESS] Deleting: {f}")
        f.unlink()
        cleared_count += 1
    print(f"[PROCESS] Cleared {cleared_count} files")

    print("[PROCESS] Processing RSS entries...")
    initial_image_files, processed_count_initial = process_rss_entries(selected_url, start_entry_index=0, num_entries=3) # Initial 3
    feed_entry_counts[selected_url] += processed_count_initial # Update count
    
    print(f"[PROCESS] Generated {len(initial_image_files)} images: {initial_image_files}")
    print(f"[PROCESS] Processed count: {processed_count_initial}")
    print(f"[PROCESS] Feed entry counts: {feed_entry_counts}")

    all_image_files = initial_image_files # Only send initial 3 to gallery

    print(f"[PROCESS] Redirecting to gallery with {len(all_image_files)} images...")
    return redirect(url_for('gallery', image_files=json.dumps(all_image_files), rss_url=selected_url, processed_count=feed_entry_counts[selected_url])) # Pass filenames and rss_url

@app.route('/load_more_images')
def load_more_images():
    rss_url = request.args.get('rss_url')
    if not rss_url:
        return jsonify({"error": "RSS URL is missing"}), 400

    start_index = feed_entry_counts.get(rss_url, 0)
    generated_image_files, processed_count = process_rss_entries(rss_url, start_entry_index=start_index, num_entries=3) # Load next 3
    feed_entry_counts[rss_url] += processed_count # Update count

    return jsonify({"image_files": generated_image_files, "processed_count": feed_entry_counts[rss_url]})


@app.route('/gallery')
def gallery():
    image_files_json = request.args.get('image_files', '[]')
    image_files = json.loads(image_files_json)
    rss_url = request.args.get('rss_url') # Get rss_url
    processed_count = int(request.args.get('processed_count', 0))


    return render_template('gallery.html', image_files=image_files, rss_url=rss_url, processed_count=processed_count) # Pass to template

# =============================================================================
# Main Entry Point
# =============================================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
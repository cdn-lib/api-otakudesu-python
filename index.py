import base64
import json
import asyncio
import uvicorn
import os
import re
import psutil
import platform
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
from bs4 import BeautifulSoup

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASEURL = os.getenv("BASEURL", "https://otakudesu.best")
ANOBOY = os.getenv("ANOBOY", "https://anoboy.be")
PORT = int(os.getenv("PORT", 3000))
EXTERNAL_API_URL = 'https://anime-api-ebon-mu.vercel.app/api'

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": f"{BASEURL}/",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br"
}

client = httpx.AsyncClient(
    headers=HEADERS, 
    timeout=15.0, 
    follow_redirects=True,
    http2=True,
    verify=False
)

anime_cache = {}

async def fetch_url(url: str, method: str = "GET", data: dict = None, retries: int = 3) -> httpx.Response:
    delay = 1.0
    for attempt in range(retries):
        try:
            if method == "POST":
                response = await client.post(url, data=data)
            else:
                response = await client.get(url)
            response.raise_for_status()
            return response
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            should_retry = False
            if isinstance(e, httpx.HTTPStatusError):
                if e.response.status_code in [403, 429]:
                    should_retry = True
            else:
                should_retry = True
            
            if not should_retry or attempt == retries - 1:
                raise e
            await asyncio.sleep(delay * (2 ** attempt))

def parse_pagination(soup: BeautifulSoup, anoboy: bool = False):
    if anoboy:
        current_el = soup.select_one('.wp-pagenavi .current')
        last_el = soup.select_one('.wp-pagenavi .page.larger:last-child')
        
        if not current_el: return False
        current_page = int(current_el.text)
        last_visible_page = int(last_el.text) if last_el else current_page
    else:
        current_el = soup.select_one('.pagination .pagenavix .page-numbers.current')
        prev_last_el = soup.select_one('.pagination .pagenavix .page-numbers:last-child')
        
        if not current_el: return False
        current_page = int(current_el.text)
        
        last_val = current_page
        if prev_last_el:
            prev = prev_last_el.find_previous('a', class_='page-numbers')
            if prev:
                try:
                    last_val = int(prev.text)
                except:
                    pass
        last_visible_page = max(current_page, last_val)

    has_next = current_page < last_visible_page
    has_prev = current_page > 1
    
    return {
        "current_page": current_page,
        "last_visible_page": last_visible_page,
        "has_next_page": has_next,
        "next_page": current_page + 1 if has_next else None,
        "has_previous_page": has_prev,
        "previous_page": current_page - 1 if has_prev else None
    }

def clean_url(url: str, prefix: str) -> str:
    if not url: return ""
    return url.replace(prefix, "").replace("/", "")

def map_genres(html_str: str):
    soup = BeautifulSoup(html_str, 'lxml')
    result = []
    for a in soup.select('a'):
        result.append({
            "name": a.text,
            "slug": clean_url(a['href'], f"{BASEURL}/genres/"),
            "otakudesu_url": a['href']
        })
    return result

def scrape_ongoing(html: str):
    soup = BeautifulSoup(html, 'lxml')
    result = []
    items = soup.select('li')
    
    for item in items:
        detpost = item.select_one('.detpost')
        if not detpost: continue

        thumb = detpost.select_one('.thumb a')
        img = detpost.select_one('.thumb .thumbz img')
        title = detpost.select_one('.thumb .thumbz .jdlflm')
        ep = detpost.select_one('.epz')
        day = detpost.select_one('.epztipe')
        date = detpost.select_one('.newnime')
        
        if not thumb: continue
        
        result.append({
            "title": title.text if title else "",
            "slug": clean_url(thumb['href'], f"{BASEURL}/anime/"),
            "poster": img['src'] if img else "",
            "current_episode": ep.text.strip() if ep else "",
            "release_day": day.text.strip() if day else "",
            "newest_release_date": date.text if date else "",
            "otakudesu_url": thumb['href']
        })
    return result

def scrape_complete(html: str):
    soup = BeautifulSoup(html, 'lxml')
    result = []
    items = soup.select('li')
    
    for item in items:
        detpost = item.select_one('.detpost')
        if not detpost: continue
        
        thumb = detpost.select_one('.thumb a')
        img = detpost.select_one('.thumb .thumbz img')
        title = detpost.select_one('.thumb .thumbz .jdlflm')
        ep = detpost.select_one('.epz')
        rating = detpost.select_one('.epztipe')
        date = detpost.select_one('.newnime')
        
        if not thumb: continue
        
        result.append({
            "title": title.text if title else "",
            "slug": clean_url(thumb['href'], f"{BASEURL}/anime/"),
            "poster": img['src'] if img else "",
            "episode_count": ep.text.strip().replace(" Episode", "") if ep else "",
            "rating": rating.text.strip() if rating else "",
            "last_release_date": date.text if date else "",
            "otakudesu_url": thumb['href']
        })
    return result

def scrape_anime_episodes(html: str):
    soup = BeautifulSoup(html, 'lxml')
    result = []
    
    ep_list_container = soup.select('.episodelist')
    if len(ep_list_container) < 2: return []
    
    items = ep_list_container[1].select('ul li')
    
    for item in items:
        a = item.select_one('span:first-child a')
        if a:
            result.append({
                "episode": a.text,
                "slug": clean_url(a['href'], f"{BASEURL}/episode/"),
                "otakudesu_url": a['href']
            })
    return result

def scrape_anime_details(html: str):
    soup = BeautifulSoup(html, 'lxml')
    
    info_container = soup.select_one('.infozin .infozingle')
    if not info_container: return None

    ps = info_container.select('p')
    info = {}
    for p in ps:
        text = p.text
        if ":" in text:
            parts = text.split(":", 1)
            key = parts[0].strip().lower()
            val = parts[1].strip()
            info[key] = val

    poster = soup.select_one('.fotoanime img')
    synopsis = soup.select_one('.sinopc')
    
    genres_html = ""
    last_p = info_container.select_one('p:last-child span')
    if last_p: genres_html = str(last_p)
    genres = map_genres(genres_html)

    batch_link = soup.select_one('.venser #serieslist ~ .episodelist ul li:first-child span:first-child a')
    batch = None
    if batch_link and "batch" in batch_link['href']:
        uploaded = soup.select_one('.venser #serieslist ~ .episodelist ul li:first-child span.zeebr:first-child')
        batch = {
            "slug": clean_url(batch_link['href'], f"{BASEURL}/batch/"),
            "otakudesu_url": batch_link['href'],
            "uploaded_at": uploaded.text if uploaded else ""
        }

    recommendations = []
    for rec in soup.select('#recommend-anime-series .isi-recommend-anime-series .isi-konten'):
        rec_a = rec.select_one('a')
        rec_img = rec.select_one('.isi-anime img')
        rec_title = rec.select_one('.judul-anime')
        if rec_a:
            recommendations.append({
                "title": rec_title.text.strip() if rec_title else "",
                "slug": clean_url(rec_a['href'], f"{BASEURL}/anime/"),
                "poster": rec_img['src'] if rec_img else "",
                "otakudesu_url": rec_a['href']
            })

    return {
        "title": info.get('judul'),
        "japanese_title": info.get('japanese'),
        "poster": poster['src'] if poster else "",
        "rating": info.get('skor'),
        "produser": info.get('produser'),
        "type": info.get('tipe'),
        "status": info.get('status'),
        "episode_count": info.get('total episode'),
        "duration": info.get('durasi'),
        "release_date": info.get('tanggal rilis'),
        "studio": info.get('studio'),
        "genres": genres,
        "synopsis": synopsis.get_text("\n", strip=True) if synopsis else "",
        "batch": batch,
        "episode_lists": scrape_anime_episodes(html),
        "recommendations": recommendations
    }

async def get_external_stream(title, episode_num):
    try:
        r_search = await client.get(f"{EXTERNAL_API_URL}/search", params={"keyword": title})
        d_search = r_search.json()
        if not d_search.get("success") or not d_search["results"]: return {}
        
        anime_id = d_search["results"][0]["id"]
        r_eps = await client.get(f"{EXTERNAL_API_URL}/episodes/{anime_id}")
        d_eps = r_eps.json()
        if not d_eps.get("success"): return {}
        
        episodes = d_eps["results"]["episodes"]
        target = next((e for e in episodes if e["episode_no"] == episode_num), None)
        if not target: return {}
        
        r_stream = await client.get(f"{EXTERNAL_API_URL}/stream", params={"id": target["id"]})
        d_stream = r_stream.json()
        
        if not d_stream.get("success") or not d_stream["results"]["streamingLink"]: return {}
        
        links = d_stream["results"]["streamingLink"]
        hls = next((l for l in links if l["link"]["type"] == "hls"), None)
        
        if hls:
            r_m3u8 = await client.get(hls["link"]["file"])
            content = r_m3u8.text
            resolutions = {}
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if 'RESOLUTION=' in line:
                    match = re.search(r'RESOLUTION=\d+x(\d+)', line)
                    if match:
                        res_key = match.group(1) + 'p'
                        url_line = lines[i+1]
                        if url_line and not url_line.startswith('#'):
                            resolutions[res_key] = url_line
            resolutions["Auto"] = hls["link"]["file"]
            return resolutions
        else:
            mp4 = next((l for l in links if l["link"]["type"] == "mp4"), None)
            if mp4: return {"Default": mp4["link"]["file"]}
            
        return {}
    except:
        return {}

async def process_ajax_payloads(action1, action2, video_data):
    results = {}
    url = f"{BASEURL}/wp-admin/admin-ajax.php"
    
    for res, payload in video_data.items():
        try:
            form1 = {
                "id": payload.get("id"),
                "i": payload.get("i"),
                "q": payload.get("q"),
                "action": action1
            }
            r1 = await fetch_url(url, "POST", form1)
            nonce = r1.json().get("data")

            form2 = {
                "id": payload.get("id"),
                "i": payload.get("i"),
                "q": payload.get("q"),
                "action": action2,
                "nonce": nonce
            }
            r2 = await fetch_url(url, "POST", form2)
            html_str = base64.b64decode(r2.json().get("data")).decode('utf-8')
            
            iframe_soup = BeautifulSoup(html_str, 'lxml')
            iframe = iframe_soup.select_one("iframe")
            if iframe:
                results[res] = iframe['src']
        except:
            continue
            
    return results

async def get_local_stream_quality(soup: BeautifulSoup):
    actions = []
    scripts = soup.select("script")
    for script in scripts:
        if script.string:
            matches = re.findall(r'action\s*:\s*"([a-z0-9]+)"', script.string, re.IGNORECASE)
            actions.extend(matches)

    if len(actions) < 2:
        return {}

    unique_actions = list(set(actions))
    if len(unique_actions) >= 2:
         action_initial = unique_actions[1]
         action_final = unique_actions[0]
    else:
        return {}

    payloads = {}
    stream_label = soup.select_one('.mirrorstream')
    
    if not stream_label: return {}

    for q in ["m360p", "m480p", "m720p"]:
        items = stream_label.select(f'ul.{q} li a')
        selected = None
        
        for item in items:
            txt = item.text.lower()
            if "desu" in txt or "drain" in txt or "ondesu" in txt:
                selected = item
                break
        
        if not selected and items:
            selected = items[0]

        if selected and selected.get('data-content'):
            try:
                json_str = base64.b64decode(selected['data-content']).decode('utf-8')
                payloads[q.replace('m', '')] = json.loads(json_str)
            except:
                pass

    return await process_ajax_payloads(action_initial, action_final, payloads)

def scrape_search(html: str):
    soup = BeautifulSoup(html, 'lxml')
    results = []
    
    for li in soup.select('.chivsrc li'):
        img = li.select_one('img')
        title_a = li.select_one('h2 a')
        
        if not title_a: continue
        
        sets = li.select('.set')
        genres = []
        status = "Unknown"
        rating = "N/A"
        
        for s in sets:
            txt = s.text
            if "Genres" in txt:
                for a in s.select('a'):
                    genres.append(a.text)
            elif "Status" in txt:
                status = txt.replace("Status", "").replace(":", "").strip()
            elif "Rating" in txt:
                rating = txt.replace("Rating", "").replace(":", "").strip()

        results.append({
            "title": title_a.text.strip(),
            "slug": clean_url(title_a['href'], f"{BASEURL}/anime/"),
            "poster": img['src'] if img else "",
            "genres": genres,
            "status": status,
            "rating": rating,
            "url": title_a['href']
        })
    return results

def scrape_batch(html: str):
    soup = BeautifulSoup(html, 'lxml')
    batch_title = soup.select_one('.download2 .batchlink h4')
    
    download_urls = []
    url_groups = soup.select('.download2 .batchlink ul li')
    
    for li in url_groups:
        res = li.select_one('strong')
        size = li.select_one('i')
        links = []
        for a in li.select('a'):
            links.append({
                "provider": a.text,
                "url": a['href']
            })
        download_urls.append({
            "resolution": res.text.strip() if res else "",
            "file_size": size.text.strip() if size else "",
            "urls": links
        })

    return {
        "batch": batch_title.text if batch_title else "",
        "download_urls": download_urls
    }

@app.get("/")
async def root():
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    return {
        "status": "OK",
        "message": "Scraper API otakudesu (Python Version)",
        "system": {
            "ram": {
                "totalGB": f"{mem.total / (1024**3):.2f}",
                "freeGB": f"{mem.available / (1024**3):.2f}"
            },
            "os": {
                "platform": platform.system(),
                "release": platform.release(),
                "arch": platform.machine()
            },
            "disk": {
                "size": f"{disk.total / (1024**3):.2f}G",
                "used": f"{disk.used / (1024**3):.2f}G",
                "free": f"{disk.free / (1024**3):.2f}G",
                "percent": f"{disk.percent}%"
            }
        }
    }

@app.get("/v1/home")
async def home():
    try:
        r = await fetch_url(BASEURL)
        soup = BeautifulSoup(r.text, 'lxml')
        
        ongoing_el = str(soup.select_one('.venutama .rseries .rapi:first-child .venz ul'))
        complete_el = str(soup.select_one('.venutama .rseries .rapi:last-child .venz ul'))
        
        return {
            "status": "Ok",
            "data": {
                "ongoing_anime": scrape_ongoing(ongoing_el),
                "complete_anime": scrape_complete(complete_el)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/search/{keyword}")
async def search(keyword: str):
    try:
        url = f"{BASEURL}/?s={keyword}&post_type=anime"
        r = await fetch_url(url)
        return {"status": "Ok", "data": scrape_search(r.text)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/ongoing-anime")
@app.get("/v1/ongoing-anime/{page}")
async def ongoing(page: int = 1):
    try:
        url = f"{BASEURL}/ongoing-anime/page/{page}"
        r = await fetch_url(url)
        soup = BeautifulSoup(r.text, 'lxml')
        
        ul = str(soup.select_one('.venutama .rseries .rapi .venz ul'))
        
        return {
            "status": "Ok",
            "data": scrape_ongoing(ul),
            "pagination": parse_pagination(soup)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/complete-anime")
@app.get("/v1/complete-anime/{page}")
async def complete(page: int = 1):
    try:
        url = f"{BASEURL}/complete-anime/page/{page}"
        r = await fetch_url(url)
        soup = BeautifulSoup(r.text, 'lxml')
        
        ul = str(soup.select_one('.venutama .rseries .rapi .venz ul'))
        
        return {
            "status": "Ok",
            "data": scrape_complete(ul),
            "pagination": parse_pagination(soup)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/anime/{slug}")
async def anime_detail(slug: str):
    try:
        url = f"{BASEURL}/anime/{slug}"
        r = await fetch_url(url)
        data = scrape_anime_details(r.text)
        if not data: raise HTTPException(status_code=404, detail="Not Found")
        return {"status": "Ok", "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/episode/{slug}")
async def episode_detail(slug: str):
    try:
        url = f"{BASEURL}/episode/{slug}"
        r = await fetch_url(url)
        soup = BeautifulSoup(r.text, 'lxml')
        
        title_el = soup.select_one('.venutama .posttl')
        if not title_el: raise HTTPException(status_code=404)
        episode_title = title_el.text

        stream_urls = {}
        
        try:
            clean_title = re.sub(r'Episode\s+\d+.*', '', episode_title, flags=re.IGNORECASE)
            clean_title = clean_title.replace('Subtitle Indonesia', '').strip()
            ep_match = re.search(r'Episode\s+(\d+)', episode_title, re.IGNORECASE)
            ep_num = int(ep_match.group(1)) if ep_match else 1
            
            stream_urls = await get_external_stream(clean_title, ep_num)
        except:
            pass
            
        if not stream_urls:
            stream_urls = await get_local_stream_quality(soup)

        default_iframe = soup.select_one('#pembed iframe')
        default_url = default_iframe['src'] if default_iframe else None
        
        valid_default = None
        if default_url and 'desustream' in default_url:
            valid_default = default_url
            
        if not stream_urls and valid_default:
             stream_urls = {"Default": valid_default}

        download_urls = []
        for dl_div in soup.select('.download ul'):
            res = dl_div.find_previous('strong') 
            links = []
            for a in dl_div.select('a'):
                links.append({"provider": a.text, "url": a['href']})
            download_urls.append({
                "resolution": res.text if res else "Unknown",
                "urls": links
            })

        prev_ep = soup.select_one('.flir a[href*="/episode/"]')
        next_ep = soup.select_one('.flir a:last-child[href*="/episode/"]')
        
        anime_link = soup.select_one('.flir a[href*="/anime/"]')
        anime_info = {}
        if anime_link:
            anime_info = {
                "slug": clean_url(anime_link['href'], f"{BASEURL}/anime/"),
                "otakudesu_url": anime_link['href']
            }

        return {
            "status": "Ok",
            "data": {
                "episode": episode_title,
                "anime": anime_info,
                "stream_url": stream_urls.get('480p') or stream_urls.get('Default') or (list(stream_urls.values())[0] if stream_urls else valid_default),
                "stream_list": stream_urls,
                "download_urls": {"mp4": download_urls}, 
                "has_next_episode": bool(next_ep),
                "next_episode": clean_url(next_ep['href'], f"{BASEURL}/episode/") if next_ep else None,
                "has_previous_episode": bool(prev_ep),
                "previous_episode": clean_url(prev_ep['href'], f"{BASEURL}/episode/") if prev_ep else None
            }
        }

    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/anime/{slug}/episodes/{episode}")
async def smart_episode(slug: str, episode: int):
    global anime_cache
    try:
        ep_list = anime_cache.get(slug)
        if not ep_list:
            url = f"{BASEURL}/anime/{slug}"
            r = await fetch_url(url)
            data = scrape_anime_details(r.text)
            if not data: raise HTTPException(status_code=404)
            ep_list = data['episode_lists']
            anime_cache[slug] = ep_list

        target_slug = None
        
        clean_list = []
        for ep in ep_list:
            match = re.search(r'Episode\s+(\d+)', ep['episode'])
            ep_num = int(match.group(1)) if match else 0
            clean_list.append({"num": ep_num, "slug": ep['slug']})
            
        # Logic reconstruction from Node
        if clean_list:
            target = next((x for x in clean_list if x["num"] == episode), None)
            if target:
                target_slug = target["slug"]
            else:
                 # Fallback logic mimicking Node's slug construction
                 top_prefix = clean_list[0]["slug"].split('-episode-')[0]
                 target_slug = f"{top_prefix}-episode-{episode}-sub-indo"

        if not target_slug:
            return {"status": "Error", "message": "Episode not found"}

        return await episode_detail(target_slug)
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/batch/{slug}")
async def batch_by_slug(slug: str):
    try:
        url = f"{BASEURL}/batch/{slug}"
        r = await fetch_url(url)
        return {"status": "Ok", "data": scrape_batch(r.text)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/anime/{slug}/batch")
async def batch_by_anime(slug: str):
    try:
        url = f"{BASEURL}/anime/{slug}"
        r = await fetch_url(url)
        soup = BeautifulSoup(r.text, 'lxml')
        batch_link = soup.select_one('.venser #serieslist ~ .episodelist ul li:first-child span:first-child a')
        
        if not batch_link or "batch" not in batch_link['href']:
             raise HTTPException(status_code=404, detail="No batch available")
             
        batch_slug = clean_url(batch_link['href'], f"{BASEURL}/batch/")
        return await batch_by_slug(batch_slug)
    except Exception as e:
         raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/movies/{page}")
async def movies(page: int = 1):
    try:
        url = f"{ANOBOY}/category/anime-movie/page/{page}"
        r = await fetch_url(url)
        soup = BeautifulSoup(r.text, 'lxml')
        
        movies = []
        for a in soup.select('a[rel="bookmark"]'):
            href = a['href']
            parts = href.replace(ANOBOY, '').split('/')
            if len(parts) < 4: continue
            
            img = a.find('amp-img')
            movies.append({
                "title": a.get('title'),
                "years": parts[1],
                "month": parts[2],
                "slug": parts[3],
                "poster": ANOBOY + img['src'] if img else "",
                "otakudesu_url": href
            })
            
        return {"status": "Ok", "data": {"movies": movies, "pagination": parse_pagination(soup, True)}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/movies/{year}/{month}/{slug}")
async def single_movie(year: str, month: str, slug: str):
    try:
        url = f"{ANOBOY}/{year}/{month}/{slug}"
        r = await fetch_url(url)
        soup = BeautifulSoup(r.text, 'lxml')
        
        title = soup.select_one('.unduhan h3')
        poster = soup.select_one('.unduhan amp-img')
        
        download_urls = {'480p': [], '720p': [], '1080p': []}
        
        for div in soup.select('.download .ud .udl'):
            label = div.text.lower()
            a = div.select_one('a')
            if not a or a['href'] == 'none': continue
            
            link = a['href']
            if '480' in label: download_urls['480p'].append(link)
            elif '720' in label: download_urls['720p'].append(link)
            elif '1080' in label or '1k' in label: download_urls['1080p'].append(link)
            
        stream_url = next((u for u in download_urls['480p'] if 'mp4upload' in u), None)

        return {
            "status": "Ok", 
            "data": {
                "title": title.text.split('sub')[0].strip().lower() if title else "",
                "poster": ANOBOY + poster['src'] if poster else "",
                "download_urls": download_urls,
                "stream_url": stream_url
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)

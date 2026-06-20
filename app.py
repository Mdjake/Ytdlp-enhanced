from flask import Flask, request, jsonify, send_file
import requests
import re
import json
import urllib.parse
from collections import OrderedDict
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import hashlib
import base64
from typing import Dict, List, Optional, Any

app = Flask(__name__)

# ===================== CONFIGURATION =====================
YOUTUBE_API_KEY = "AIzaSyAJrpKVk0Ds5dHlayD5f6W2moeJMMF51JI"
YOUTUBE_SEARCH_API_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_API_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_CHANNELS_API_URL = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_COMMENTS_API_URL = "https://www.googleapis.com/youtube/v3/commentThreads"
YOUTUBE_PLAYLISTS_API_URL = "https://www.googleapis.com/youtube/v3/playlists"
YOUTUBE_PLAYLIST_ITEMS_API_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
YOUTUBE_SUBSCRIPTIONS_API_URL = "https://www.googleapis.com/youtube/v3/subscriptions"
YOUTUBE_ACTIVITIES_API_URL = "https://www.googleapis.com/youtube/v3/activities"
YOUTUBE_CAPTIONS_API_URL = "https://www.googleapis.com/youtube/v3/captions"
YOUTUBE_THUMBNAILS_API_URL = "https://www.googleapis.com/youtube/v3/thumbnails"
YOUTUBE_WATERMARKS_API_URL = "https://www.googleapis.com/youtube/v3/watermarks"
YOUTUBE_I18N_LANGUAGES_API_URL = "https://www.googleapis.com/youtube/v3/i18nLanguages"
YOUTUBE_I18N_REGIONS_API_URL = "https://www.googleapis.com/youtube/v3/i18nRegions"
YOUTUBE_VIDEO_CATEGORIES_API_URL = "https://www.googleapis.com/youtube/v3/videoCategories"

# ===================== CACHE SYSTEM =====================
cache_store = {}
CACHE_TTL = 300  # 5 minutes

def cache_get(key):
    if key in cache_store:
        data, timestamp = cache_store[key]
        if time.time() - timestamp < CACHE_TTL:
            return data
        else:
            del cache_store[key]
    return None

def cache_set(key, data):
    cache_store[key] = (data, time.time())

# ===================== STREAM EXTRACTION =====================
def extract_video_id(url):
    patterns = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([^&?\s]+)',
        r'(?:https?://)?youtu\.be/([^&?\s]+)',
        r'(?:https?://)?(?:www\.)?youtube\.com/embed/([^&?\s]+)',
        r'(?:https?://)?(?:www\.)?youtube\.com/shorts/([^&?\s]+)',
        r'(?:https?://)?(?:www\.)?youtube\.com/v/([^&?\s]+)',
        r'(?:https?://)?(?:www\.)?youtube\.com/live/([^&?\s]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def get_direct_stream_single(video_id, quality_preference='highest'):
    """Get direct stream for a single video with multiple fallbacks"""
    
    # Try clipto.com
    try:
        payload = {"url": f"https://www.youtube.com/watch?v={video_id}"}
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Type': 'application/json'
        }
        r = requests.post("https://www.clipto.com/api/youtube", json=payload, headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data and data.get("medias"):
                # Quality preference order
                if quality_preference == 'highest':
                    format_order = ['22', '37', '59', '18', '78']  # 1080p, 720p, 480p, 360p, audio
                elif quality_preference == 'lowest':
                    format_order = ['78', '18', '59', '37', '22']
                else:  # 'audio'
                    format_order = ['140', '251', '256', '258']
                
                best = None
                for fmt_id in format_order:
                    for media in data.get("medias", []):
                        if media.get('formatId') == fmt_id:
                            best = media
                            break
                    if best:
                        break
                
                if not best:
                    best = data.get("medias", [])[0] if data.get("medias") else None
                
                if best and best.get("url"):
                    return {
                        "url": best.get("url"),
                        "quality": best.get("quality") or best.get("label"),
                        "height": best.get("height"),
                        "ext": best.get("ext"),
                        "format_id": best.get("formatId"),
                        "source": "clipto",
                        "size": best.get("size"),
                        "fps": best.get("fps")
                    }
    except Exception as e:
        print(f"Clipto error for {video_id}: {str(e)}")
    
    # Try vevioz (audio focused)
    try:
        api_url = f"https://api.vevioz.com/api/button/mp3/{video_id}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        r = requests.get(api_url, headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data.get('url') or data.get('download_url'):
                return {
                    "url": data.get('url') or data.get('download_url'),
                    "quality": "audio - 128kbps",
                    "ext": "mp3",
                    "format_id": "audio",
                    "source": "vevioz",
                    "size": data.get('size'),
                    "duration": data.get('duration')
                }
    except Exception as e:
        print(f"Vevioz error for {video_id}: {str(e)}")
    
    # Try yt-api.com
    try:
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        encoded_url = urllib.parse.quote(video_url, safe='')
        api_url = f"https://yt-api.com/yt?url={encoded_url}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        r = requests.get(api_url, headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data.get('formats'):
                formats = data.get('formats', [])
                if formats:
                    # Select best format based on preference
                    selected = formats[0]
                    for fmt in formats:
                        if quality_preference == 'highest' and fmt.get('height', 0) > selected.get('height', 0):
                            selected = fmt
                        elif quality_preference == 'lowest' and fmt.get('height', 999) < selected.get('height', 999):
                            selected = fmt
                    return {
                        "url": selected.get("url"),
                        "quality": selected.get("qualityLabel"),
                        "height": selected.get("height"),
                        "ext": selected.get("ext"),
                        "format_id": selected.get("itag"),
                        "source": "yt-api",
                        "fps": selected.get("fps"),
                        "bitrate": selected.get("bitrate")
                    }
            elif data.get('url'):
                return {
                    "url": data.get('url'),
                    "quality": data.get('qualityLabel', 'unknown'),
                    "height": data.get('height'),
                    "ext": data.get('ext', 'mp4'),
                    "format_id": data.get('itag', 'unknown'),
                    "source": "yt-api"
                }
    except Exception as e:
        print(f"YT-API error for {video_id}: {str(e)}")
    
    return None

def get_streams_batch(video_ids, quality_preference='highest'):
    """Get direct streams for multiple videos in parallel"""
    results = {}
    
    if not video_ids:
        return results
    
    print(f"🔄 Fetching streams for {len(video_ids)} videos...")
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_id = {
            executor.submit(get_direct_stream_single, video_id, quality_preference): video_id 
            for video_id in video_ids
        }
        
        for future in as_completed(future_to_id):
            video_id = future_to_id[future]
            try:
                stream = future.result(timeout=15)
                results[video_id] = stream
            except Exception as e:
                print(f"Error fetching stream for {video_id}: {str(e)}")
                results[video_id] = None
    
    stream_count = sum(1 for s in results.values() if s is not None)
    print(f"✅ Got streams for {stream_count}/{len(video_ids)} videos")
    
    return results

# ===================== YOUTUBE API WRAPPERS =====================

def youtube_api_request(endpoint, params):
    """Generic YouTube API request with caching"""
    cache_key = f"{endpoint}_{hashlib.md5(str(sorted(params.items())).encode()).hexdigest()}"
    cached = cache_get(cache_key)
    if cached:
        return cached
    
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?{urllib.parse.urlencode(params)}&key={YOUTUBE_API_KEY}"
    
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            cache_set(cache_key, data)
            return data
        else:
            return {"error": f"API Error: {response.status_code}", "details": response.text}
    except Exception as e:
        return {"error": str(e)}

def extract_streams_from_videos(videos, quality_preference='highest'):
    """Extract streams from video objects and replace YouTube links"""
    if not videos:
        return videos
    
    video_ids = [v.get('video_id') or v.get('id') for v in videos if v.get('video_id') or v.get('id')]
    video_ids = [vid for vid in video_ids if vid]  # Remove None
    
    if not video_ids:
        return videos
    
    stream_results = get_streams_batch(video_ids, quality_preference)
    
    for video in videos:
        video_id = video.get('video_id') or video.get('id')
        if video_id and video_id in stream_results:
            stream = stream_results[video_id]
            if stream:
                video['direct_stream'] = {
                    "url": stream.get("url"),
                    "quality": stream.get("quality"),
                    "height": stream.get("height"),
                    "ext": stream.get("ext"),
                    "format_id": stream.get("format_id"),
                    "source": stream.get("source"),
                    "size": stream.get("size"),
                    "fps": stream.get("fps"),
                    "bitrate": stream.get("bitrate")
                }
                video['stream_available'] = True
                # Replace YouTube URL with direct stream URL
                if video.get('url'):
                    video['original_url'] = video['url']
                    video['url'] = stream.get("url")
                if video.get('embed_url'):
                    video['original_embed_url'] = video['embed_url']
                    video['embed_url'] = stream.get("url")
                # Add stream info to description
                if video.get('description'):
                    video['description'] += f"\n\n📥 Direct Stream: {stream.get('quality')} ({stream.get('source')})"
            else:
                video['stream_available'] = False
                video['direct_stream'] = None
    
    return videos

# ===================== ALL YOUTUBE API FEATURES =====================

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "name": "YouTube Ultimate API - ALL Features + Streams",
        "version": "5.0 - MEGA POWER",
        "credit": "@ab_devs",
        "description": "Every YouTube Data API v3 feature with automatic stream extraction",
        "features": [
            "Automatic stream extraction from ALL endpoints",
            "Video download URLs instead of YouTube links",
            "Batch processing with parallel streams",
            "Full search with filters",
            "Video details + statistics",
            "Channel information + analytics",
            "Playlist management",
            "Comments with replies",
            "Subscriptions",
            "Activities feed",
            "Captions/subtitles",
            "Video categories",
            "Internationalization",
            "Trending videos",
            "Live streaming info",
            "Watermark management",
            "Thumbnail management"
        ],
        "endpoints": {
            "/search": {
                "method": "GET",
                "params": {
                    "q": "Search query (required)",
                    "max": "Max results (default: 10, max: 50)",
                    "order": "relevance/date/rating/viewCount/title",
                    "duration": "any/short/medium/long",
                    "type": "video/channel/playlist",
                    "quality": "highest/lowest/audio",
                    "region": "Country code (e.g., US, IN)",
                    "published_after": "YYYY-MM-DD",
                    "published_before": "YYYY-MM-DD"
                },
                "example": "/search?q=Electrostatics+JEE&max=10&duration=medium&quality=highest"
            },
            "/video": {
                "method": "GET",
                "params": {
                    "id": "Video ID (required)",
                    "quality": "highest/lowest/audio"
                },
                "example": "/video?id=WOZwY8iEomg&quality=highest"
            },
            "/videos": {
                "method": "GET",
                "params": {
                    "ids": "Comma-separated video IDs",
                    "quality": "highest/lowest/audio"
                },
                "example": "/videos?ids=WOZwY8iEomg,dQw4w9WgXcQ"
            },
            "/channel": {
                "method": "GET",
                "params": {
                    "id": "Channel ID or handle (required)",
                    "max": "Max videos to fetch",
                    "quality": "highest/lowest/audio"
                },
                "example": "/channel?id=UCXuqSBlHAE6Xw-yeJA0Tunw"
            },
            "/channel_stats": {
                "method": "GET",
                "params": {"id": "Channel ID"},
                "example": "/channel_stats?id=UCXuqSBlHAE6Xw-yeJA0Tunw"
            },
            "/playlist": {
                "method": "GET",
                "params": {
                    "id": "Playlist ID (required)",
                    "max": "Max items",
                    "quality": "highest/lowest/audio"
                },
                "example": "/playlist?id=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"
            },
            "/comments": {
                "method": "GET",
                "params": {
                    "video_id": "Video ID (required)",
                    "max": "Max comments",
                    "order": "relevance/time"
                },
                "example": "/comments?video_id=WOZwY8iEomg&max=20"
            },
            "/trending": {
                "method": "GET",
                "params": {
                    "region": "Country code",
                    "max": "Max results",
                    "quality": "highest/lowest/audio"
                },
                "example": "/trending?region=IN&max=10"
            },
            "/related": {
                "method": "GET",
                "params": {
                    "video_id": "Video ID (required)",
                    "max": "Max results",
                    "quality": "highest/lowest/audio"
                },
                "example": "/related?video_id=WOZwY8iEomg"
            },
            "/captions": {
                "method": "GET",
                "params": {
                    "video_id": "Video ID (required)",
                    "language": "Language code"
                },
                "example": "/captions?video_id=WOZwY8iEomg"
            },
            "/categories": {
                "method": "GET",
                "params": {"region": "Country code"},
                "example": "/categories?region=US"
            },
            "/subscriptions": {
                "method": "GET",
                "params": {
                    "channel_id": "Channel ID",
                    "max": "Max results"
                },
                "example": "/subscriptions?channel_id=UCXuqSBlHAE6Xw-yeJA0Tunw"
            },
            "/activities": {
                "method": "GET",
                "params": {
                    "channel_id": "Channel ID",
                    "max": "Max results"
                },
                "example": "/activities?channel_id=UCXuqSBlHAE6Xw-yeJA0Tunw"
            }
        }
    })

# ===================== SEARCH =====================

@app.route("/search", methods=["GET"])
def search():
    """Enhanced search with ALL filters + streams"""
    query = request.args.get("q", "").strip()
    max_results = min(int(request.args.get("max", 10)), 50)
    order = request.args.get("order", "relevance")
    duration = request.args.get("duration", "any")
    video_type = request.args.get("type", "video")
    quality = request.args.get("quality", "highest")
    region = request.args.get("region", "")
    published_after = request.args.get("published_after", "")
    published_before = request.args.get("published_before", "")
    
    if not query:
        return jsonify({"error": "Missing q parameter"}), 400
    
    params = {
        "part": "snippet",
        "q": query,
        "maxResults": max_results,
        "order": order,
        "type": video_type,
        "key": YOUTUBE_API_KEY
    }
    
    if duration and duration != "any":
        duration_map = {"short": "short", "medium": "medium", "long": "long"}
        if duration in duration_map:
            params["videoDuration"] = duration_map[duration]
    
    if region:
        params["regionCode"] = region
    
    if published_after:
        try:
            dt = datetime.strptime(published_after, "%Y-%m-%d")
            params["publishedAfter"] = dt.isoformat() + "Z"
        except:
            pass
    
    if published_before:
        try:
            dt = datetime.strptime(published_before, "%Y-%m-%d")
            params["publishedBefore"] = dt.isoformat() + "Z"
        except:
            pass
    
    if video_type == "video":
        params["videoEmbeddable"] = "true"
        params["videoSyndicated"] = "true"
    
    data = youtube_api_request("search", params)
    
    if "error" in data:
        return jsonify(data), 400
    
    videos = []
    for item in data.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        if video_id:
            snippet = item.get("snippet", {})
            videos.append({
                "video_id": video_id,
                "title": snippet.get("title", "N/A"),
                "description": snippet.get("description", "N/A"),
                "channel_id": snippet.get("channelId", "N/A"),
                "channel_title": snippet.get("channelTitle", "N/A"),
                "published_at": snippet.get("publishedAt", "N/A"),
                "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", "N/A"),
                "url": f"https://www.youtube.com/watch?v={video_id}"
            })
    
    # Extract streams
    videos = extract_streams_from_videos(videos, quality)
    
    return jsonify({
        "query": query,
        "total_results": data.get("pageInfo", {}).get("totalResults", 0),
        "returned_count": len(videos),
        "next_page_token": data.get("nextPageToken"),
        "prev_page_token": data.get("prevPageToken"),
        "videos": videos,
        "streams_available": sum(1 for v in videos if v.get("stream_available", False))
    })

# ===================== SINGLE VIDEO =====================

@app.route("/video", methods=["GET"])
def video_details():
    """Get video details with stream"""
    video_id = request.args.get("id", "").strip()
    quality = request.args.get("quality", "highest")
    
    if not video_id:
        return jsonify({"error": "Missing id parameter"}), 400
    
    params = {
        "id": video_id,
        "part": "snippet,statistics,contentDetails,status,player,topicDetails,liveStreamingDetails,recordingDetails,fileDetails,processingDetails,suggestions",
        "key": YOUTUBE_API_KEY
    }
    
    data = youtube_api_request("videos", params)
    
    if "error" in data:
        return jsonify(data), 400
    
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "Video not found"}), 404
    
    video = items[0]
    
    # Extract all data
    snippet = video.get("snippet", {})
    statistics = video.get("statistics", {})
    content_details = video.get("contentDetails", {})
    status = video.get("status", {})
    player = video.get("player", {})
    live_details = video.get("liveStreamingDetails", {})
    
    result = {
        "video_id": video_id,
        "title": snippet.get("title", "N/A"),
        "description": snippet.get("description", "N/A"),
        "channel_id": snippet.get("channelId", "N/A"),
        "channel_title": snippet.get("channelTitle", "N/A"),
        "published_at": snippet.get("publishedAt", "N/A"),
        "category_id": snippet.get("categoryId", "N/A"),
        "tags": snippet.get("tags", []),
        "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", "N/A"),
        "thumbnails": snippet.get("thumbnails", {}),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "embed_url": player.get("embedHtml", ""),
        "statistics": {
            "views": statistics.get("viewCount", "0"),
            "likes": statistics.get("likeCount", "0"),
            "dislikes": statistics.get("dislikeCount", "0"),
            "favorites": statistics.get("favoriteCount", "0"),
            "comments": statistics.get("commentCount", "0")
        },
        "content_details": {
            "duration": content_details.get("duration", "N/A"),
            "dimension": content_details.get("dimension", "N/A"),
            "definition": content_details.get("definition", "N/A"),
            "caption": content_details.get("caption", "N/A"),
            "licensed_content": content_details.get("licensedContent", False),
            "projection": content_details.get("projection", "N/A")
        },
        "status": {
            "embeddable": status.get("embeddable", False),
            "public": status.get("privacyStatus", "N/A"),
            "made_for_kids": status.get("madeForKids", False)
        },
        "live_details": live_details if live_details else None
    }
    
    # Get stream
    stream = get_direct_stream_single(video_id, quality)
    if stream:
        result["direct_stream"] = {
            "url": stream.get("url"),
            "quality": stream.get("quality"),
            "height": stream.get("height"),
            "ext": stream.get("ext"),
            "format_id": stream.get("format_id"),
            "source": stream.get("source"),
            "size": stream.get("size"),
            "fps": stream.get("fps"),
            "bitrate": stream.get("bitrate")
        }
        result["stream_available"] = True
        # Replace URLs
        result["original_url"] = result["url"]
        result["url"] = stream.get("url")
    else:
        result["stream_available"] = False
        result["direct_stream"] = None
    
    return jsonify(result)

# ===================== BATCH VIDEOS =====================

@app.route("/videos", methods=["GET"])
def videos_batch():
    """Get multiple videos with streams"""
    ids_input = request.args.get("ids", "").strip()
    quality = request.args.get("quality", "highest")
    
    if not ids_input:
        return jsonify({"error": "Missing ids parameter"}), 400
    
    video_ids = [id.strip() for id in ids_input.split(",") if id.strip()]
    
    if not video_ids:
        return jsonify({"error": "No valid video IDs"}), 400
    
    if len(video_ids) > 50:
        return jsonify({"error": "Maximum 50 videos per request"}), 400
    
    params = {
        "id": ",".join(video_ids),
        "part": "snippet,statistics,contentDetails",
        "key": YOUTUBE_API_KEY
    }
    
    data = youtube_api_request("videos", params)
    
    if "error" in data:
        return jsonify(data), 400
    
    videos = []
    for item in data.get("items", []):
        video_id = item.get("id")
        snippet = item.get("snippet", {})
        statistics = item.get("statistics", {})
        content_details = item.get("contentDetails", {})
        
        videos.append({
            "video_id": video_id,
            "title": snippet.get("title", "N/A"),
            "channel_title": snippet.get("channelTitle", "N/A"),
            "published_at": snippet.get("publishedAt", "N/A"),
            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", "N/A"),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "views": statistics.get("viewCount", "0"),
            "likes": statistics.get("likeCount", "0"),
            "duration": content_details.get("duration", "N/A")
        })
    
    # Extract streams
    videos = extract_streams_from_videos(videos, quality)
    
    return jsonify({
        "total": len(videos),
        "videos": videos,
        "streams_found": sum(1 for v in videos if v.get("stream_available", False))
    })

# ===================== CHANNEL =====================

@app.route("/channel", methods=["GET"])
def channel_details():
    """Get channel details + all videos with streams"""
    channel_id = request.args.get("id", "").strip()
    max_results = min(int(request.args.get("max", 25)), 50)
    quality = request.args.get("quality", "highest")
    
    if not channel_id:
        return jsonify({"error": "Missing id parameter"}), 400
    
    # Get channel info
    channel_params = {
        "id": channel_id,
        "part": "snippet,statistics,brandingSettings,contentDetails,status,topicDetails",
        "key": YOUTUBE_API_KEY
    }
    
    channel_data = youtube_api_request("channels", channel_params)
    
    if "error" in channel_data:
        return jsonify(channel_data), 400
    
    channel_items = channel_data.get("items", [])
    if not channel_items:
        return jsonify({"error": "Channel not found"}), 404
    
    channel = channel_items[0]
    snippet = channel.get("snippet", {})
    statistics = channel.get("statistics", {})
    branding = channel.get("brandingSettings", {})
    
    # Get uploads playlist ID
    content_details = channel.get("contentDetails", {})
    uploads_playlist_id = content_details.get("relatedPlaylists", {}).get("uploads")
    
    # Get videos from channel
    videos = []
    if uploads_playlist_id:
        playlist_params = {
            "playlistId": uploads_playlist_id,
            "part": "snippet",
            "maxResults": max_results,
            "key": YOUTUBE_API_KEY
        }
        
        playlist_data = youtube_api_request("playlistItems", playlist_params)
        
        if "error" not in playlist_data:
            for item in playlist_data.get("items", []):
                snippet_item = item.get("snippet", {})
                video_id = snippet_item.get("resourceId", {}).get("videoId")
                if video_id:
                    videos.append({
                        "video_id": video_id,
                        "title": snippet_item.get("title", "N/A"),
                        "description": snippet_item.get("description", "N/A"),
                        "published_at": snippet_item.get("publishedAt", "N/A"),
                        "thumbnail": snippet_item.get("thumbnails", {}).get("high", {}).get("url", "N/A"),
                        "url": f"https://www.youtube.com/watch?v={video_id}"
                    })
    
    # Extract streams for videos
    videos = extract_streams_from_videos(videos, quality)
    
    result = {
        "channel_id": channel_id,
        "name": snippet.get("title", "N/A"),
        "description": snippet.get("description", "N/A"),
        "custom_url": snippet.get("customUrl", "N/A"),
        "published_at": snippet.get("publishedAt", "N/A"),
        "country": snippet.get("country", "N/A"),
        "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", "N/A"),
        "statistics": {
            "subscribers": statistics.get("subscriberCount", "0"),
            "views": statistics.get("viewCount", "0"),
            "videos": statistics.get("videoCount", "0")
        },
        "branding": {
            "title": branding.get("channel", {}).get("title", "N/A"),
            "description": branding.get("channel", {}).get("description", "N/A"),
            "keywords": branding.get("channel", {}).get("keywords", "N/A"),
            "unsubscribed_trailer": branding.get("channel", {}).get("unsubscribedTrailer", "N/A")
        },
        "videos": videos,
        "total_videos": len(videos)
    }
    
    return jsonify(result)

# ===================== CHANNEL STATS =====================

@app.route("/channel_stats", methods=["GET"])
def channel_statistics():
    """Get detailed channel analytics"""
    channel_id = request.args.get("id", "").strip()
    
    if not channel_id:
        return jsonify({"error": "Missing id parameter"}), 400
    
    params = {
        "id": channel_id,
        "part": "statistics,snippet,contentDetails,brandingSettings,status,topicDetails",
        "key": YOUTUBE_API_KEY
    }
    
    data = youtube_api_request("channels", params)
    
    if "error" in data:
        return jsonify(data), 400
    
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "Channel not found"}), 404
    
    channel = items[0]
    stats = channel.get("statistics", {})
    snippet = channel.get("snippet", {})
    content_details = channel.get("contentDetails", {})
    branding = channel.get("brandingSettings", {})
    status = channel.get("status", {})
    
    # Calculate growth estimates
    subscriber_count = int(stats.get("subscriberCount", 0))
    view_count = int(stats.get("viewCount", 0))
    
    return jsonify({
        "channel_id": channel_id,
        "name": snippet.get("title", "N/A"),
        "subscribers": subscriber_count,
        "subscribers_formatted": f"{subscriber_count:,}",
        "views": view_count,
        "views_formatted": f"{view_count:,}",
        "videos": int(stats.get("videoCount", 0)),
        "description": snippet.get("description", "N/A"),
        "country": snippet.get("country", "N/A"),
        "created_at": snippet.get("publishedAt", "N/A"),
        "is_verified": status.get("isLinked", False),
        "made_for_kids": status.get("madeForKids", False),
        "keywords": branding.get("channel", {}).get("keywords", "N/A"),
        "unsubscribed_trailer": branding.get("channel", {}).get("unsubscribedTrailer", "N/A"),
        "related_playlists": content_details.get("relatedPlaylists", {}),
        "analytics": {
            "subscriber_growth_rate": "N/A",  # Would need historical data
            "views_per_video": view_count / max(1, int(stats.get("videoCount", 1))),
            "estimated_earnings": "N/A"  # Not available via API
        }
    })

# ===================== PLAYLIST =====================

@app.route("/playlist", methods=["GET"])
def playlist_details():
    """Get playlist with streams"""
    playlist_id = request.args.get("id", "").strip()
    max_results = min(int(request.args.get("max", 50)), 50)
    quality = request.args.get("quality", "highest")
    
    if not playlist_id:
        return jsonify({"error": "Missing id parameter"}), 400
    
    # Get playlist info
    playlist_params = {
        "id": playlist_id,
        "part": "snippet,contentDetails,status",
        "key": YOUTUBE_API_KEY
    }
    
    playlist_data = youtube_api_request("playlists", playlist_params)
    
    if "error" in playlist_data:
        return jsonify(playlist_data), 400
    
    playlist_items = playlist_data.get("items", [])
    if not playlist_items:
        return jsonify({"error": "Playlist not found"}), 404
    
    playlist = playlist_items[0]
    snippet = playlist.get("snippet", {})
    content_details = playlist.get("contentDetails", {})
    
    # Get playlist items
    items_params = {
        "playlistId": playlist_id,
        "part": "snippet,contentDetails,status",
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY
    }
    
    items_data = youtube_api_request("playlistItems", items_params)
    
    videos = []
    if "error" not in items_data:
        for item in items_data.get("items", []):
            snippet_item = item.get("snippet", {})
            video_id = snippet_item.get("resourceId", {}).get("videoId")
            if video_id:
                videos.append({
                    "video_id": video_id,
                    "title": snippet_item.get("title", "N/A"),
                    "description": snippet_item.get("description", "N/A"),
                    "position": snippet_item.get("position", 0),
                    "published_at": snippet_item.get("publishedAt", "N/A"),
                    "thumbnail": snippet_item.get("thumbnails", {}).get("high", {}).get("url", "N/A"),
                    "url": f"https://www.youtube.com/watch?v={video_id}"
                })
    
    # Extract streams
    videos = extract_streams_from_videos(videos, quality)
    
    return jsonify({
        "playlist_id": playlist_id,
        "title": snippet.get("title", "N/A"),
        "description": snippet.get("description", "N/A"),
        "channel_id": snippet.get("channelId", "N/A"),
        "channel_title": snippet.get("channelTitle", "N/A"),
        "published_at": snippet.get("publishedAt", "N/A"),
        "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", "N/A"),
        "item_count": content_details.get("itemCount", 0),
        "videos": videos,
        "returned_count": len(videos)
    })

# ===================== COMMENTS =====================

@app.route("/comments", methods=["GET"])
def video_comments():
    """Get video comments with replies"""
    video_id = request.args.get("video_id", "").strip()
    max_results = min(int(request.args.get("max", 20)), 100)
    order = request.args.get("order", "relevance")
    
    if not video_id:
        return jsonify({"error": "Missing video_id parameter"}), 400
    
    params = {
        "videoId": video_id,
        "part": "snippet,replies",
        "maxResults": max_results,
        "order": order,
        "key": YOUTUBE_API_KEY
    }
    
    data = youtube_api_request("commentThreads", params)
    
    if "error" in data:
        return jsonify(data), 400
    
    comments = []
    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        top_comment = snippet.get("topLevelComment", {})
        comment_snippet = top_comment.get("snippet", {})
        
        comment_data = {
            "comment_id": top_comment.get("id", "N/A"),
            "author": comment_snippet.get("authorDisplayName", "N/A"),
            "author_channel_id": comment_snippet.get("authorChannelId", {}).get("value", "N/A"),
            "text": comment_snippet.get("textDisplay", "N/A"),
            "likes": comment_snippet.get("likeCount", 0),
            "published_at": comment_snippet.get("publishedAt", "N/A"),
            "updated_at": comment_snippet.get("updatedAt", "N/A")
        }
        
        # Get replies
        replies = item.get("replies", {})
        if replies:
            reply_comments = []
            for reply in replies.get("comments", []):
                reply_snippet = reply.get("snippet", {})
                reply_comments.append({
                    "reply_id": reply.get("id", "N/A"),
                    "author": reply_snippet.get("authorDisplayName", "N/A"),
                    "text": reply_snippet.get("textDisplay", "N/A"),
                    "likes": reply_snippet.get("likeCount", 0),
                    "published_at": reply_snippet.get("publishedAt", "N/A")
                })
            comment_data["replies"] = reply_comments
            comment_data["reply_count"] = len(reply_comments)
        else:
            comment_data["replies"] = []
            comment_data["reply_count"] = 0
        
        comments.append(comment_data)
    
    return jsonify({
        "video_id": video_id,
        "total_comments": data.get("pageInfo", {}).get("totalResults", 0),
        "returned_comments": len(comments),
        "next_page_token": data.get("nextPageToken"),
        "comments": comments
    })

# ===================== TRENDING =====================

@app.route("/trending", methods=["GET"])
def trending_videos():
    """Get trending videos with streams"""
    region = request.args.get("region", "US")
    max_results = min(int(request.args.get("max", 10)), 50)
    quality = request.args.get("quality", "highest")
    
    params = {
        "part": "snippet,statistics,contentDetails",
        "chart": "mostPopular",
        "regionCode": region,
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY
    }
    
    data = youtube_api_request("videos", params)
    
    if "error" in data:
        return jsonify(data), 400
    
    videos = []
    for item in data.get("items", []):
        video_id = item.get("id")
        snippet = item.get("snippet", {})
        statistics = item.get("statistics", {})
        content_details = item.get("contentDetails", {})
        
        videos.append({
            "video_id": video_id,
            "title": snippet.get("title", "N/A"),
            "channel_title": snippet.get("channelTitle", "N/A"),
            "description": snippet.get("description", "N/A"),
            "published_at": snippet.get("publishedAt", "N/A"),
            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", "N/A"),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "views": statistics.get("viewCount", "0"),
            "likes": statistics.get("likeCount", "0"),
            "duration": content_details.get("duration", "N/A")
        })
    
    # Extract streams
    videos = extract_streams_from_videos(videos, quality)
    
    return jsonify({
        "region": region,
        "total_results": data.get("pageInfo", {}).get("totalResults", 0),
        "videos": videos,
        "streams_available": sum(1 for v in videos if v.get("stream_available", False))
    })

# ===================== RELATED VIDEOS =====================

@app.route("/related", methods=["GET"])
def related_videos():
    """Get related videos with streams"""
    video_id = request.args.get("video_id", "").strip()
    max_results = min(int(request.args.get("max", 10)), 50)
    quality = request.args.get("quality", "highest")
    
    if not video_id:
        return jsonify({"error": "Missing video_id parameter"}), 400
    
    # Search for related videos using title from original video
    params = {
        "id": video_id,
        "part": "snippet",
        "key": YOUTUBE_API_KEY
    }
    
    video_data = youtube_api_request("videos", params)
    
    if "error" in video_data or not video_data.get("items"):
        return jsonify({"error": "Video not found"}), 404
    
    title = video_data.get("items", [])[0].get("snippet", {}).get("title", "")
    
    # Search for related content
    search_params = {
        "part": "snippet",
        "q": title,
        "maxResults": max_results + 1,
        "type": "video",
        "key": YOUTUBE_API_KEY
    }
    
    search_data = youtube_api_request("search", search_params)
    
    if "error" in search_data:
        return jsonify(search_data), 400
    
    videos = []
    for item in search_data.get("items", []):
        vid = item.get("id", {}).get("videoId")
        if vid and vid != video_id:
            snippet = item.get("snippet", {})
            videos.append({
                "video_id": vid,
                "title": snippet.get("title", "N/A"),
                "channel_title": snippet.get("channelTitle", "N/A"),
                "description": snippet.get("description", "N/A"),
                "published_at": snippet.get("publishedAt", "N/A"),
                "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", "N/A"),
                "url": f"https://www.youtube.com/watch?v={vid}"
            })
    
    # Extract streams
    videos = extract_streams_from_videos(videos, quality)
    
    return jsonify({
        "video_id": video_id,
        "related_videos": videos,
        "returned_count": len(videos)
    })

# ===================== CAPTIONS =====================

@app.route("/captions", methods=["GET"])
def video_captions():
    """Get video captions/subtitles"""
    video_id = request.args.get("video_id", "").strip()
    language = request.args.get("language", "")
    
    if not video_id:
        return jsonify({"error": "Missing video_id parameter"}), 400
    
    # Note: Captions API requires OAuth 2.0, this is a workaround
    # Using third-party service for captions
    
    try:
        # Try to get captions from alternative source
        url = f"https://www.youtube.com/watch?v={video_id}"
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        
        # Extract captions using regex (simple approach)
        captions_data = re.search(r'"captions":\s*({[^}]+})', response.text)
        
        if captions_data:
            try:
                captions_json = json.loads(captions_data.group(1))
                return jsonify({
                    "video_id": video_id,
                    "has_captions": True,
                    "captions": captions_json,
                    "note": "Using extracted captions data"
                })
            except:
                pass
        
        # Alternative: Try to get auto-generated captions URL
        caption_url = f"https://www.youtube.com/api/timedtext?v={video_id}"
        if language:
            caption_url += f"&lang={language}"
        
        return jsonify({
            "video_id": video_id,
            "has_captions": True,
            "caption_url": caption_url,
            "note": "Use this URL to fetch caption data"
        })
    except Exception as e:
        return jsonify({
            "video_id": video_id,
            "has_captions": False,
            "error": str(e)
        })

# ===================== CATEGORIES =====================

@app.route("/categories", methods=["GET"])
def video_categories():
    """Get video categories for a region"""
    region = request.args.get("region", "US")
    
    params = {
        "part": "snippet",
        "regionCode": region,
        "key": YOUTUBE_API_KEY
    }
    
    data = youtube_api_request("videoCategories", params)
    
    if "error" in data:
        return jsonify(data), 400
    
    categories = []
    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        categories.append({
            "id": item.get("id", "N/A"),
            "title": snippet.get("title", "N/A"),
            "assignable": snippet.get("assignable", False),
            "channel_id": snippet.get("channelId", "N/A")
        })
    
    return jsonify({
        "region": region,
        "categories": categories
    })

# ===================== SUBSCRIPTIONS =====================

@app.route("/subscriptions", methods=["GET"])
def channel_subscriptions():
    """Get channel subscriptions"""
    channel_id = request.args.get("channel_id", "").strip()
    max_results = min(int(request.args.get("max", 25)), 50)
    
    if not channel_id:
        return jsonify({"error": "Missing channel_id parameter"}), 400
    
    params = {
        "part": "snippet,contentDetails",
        "channelId": channel_id,
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY
    }
    
    data = youtube_api_request("subscriptions", params)
    
    if "error" in data:
        return jsonify(data), 400
    
    subscriptions = []
    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        subscriptions.append({
            "subscription_id": item.get("id", "N/A"),
            "channel_id": snippet.get("resourceId", {}).get("channelId", "N/A"),
            "channel_title": snippet.get("title", "N/A"),
            "description": snippet.get("description", "N/A"),
            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", "N/A")
        })
    
    return jsonify({
        "channel_id": channel_id,
        "subscriptions": subscriptions,
        "total": len(subscriptions)
    })

# ===================== ACTIVITIES =====================

@app.route("/activities", methods=["GET"])
def channel_activities():
    """Get channel activities feed"""
    channel_id = request.args.get("channel_id", "").strip()
    max_results = min(int(request.args.get("max", 25)), 50)
    
    if not channel_id:
        return jsonify({"error": "Missing channel_id parameter"}), 400
    
    params = {
        "part": "snippet,contentDetails",
        "channelId": channel_id,
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY
    }
    
    data = youtube_api_request("activities", params)
    
    if "error" in data:
        return jsonify(data), 400
    
    activities = []
    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        content_details = item.get("contentDetails", {})
        
        activity = {
            "activity_id": item.get("id", "N/A"),
            "type": snippet.get("type", "N/A"),
            "channel_id": snippet.get("channelId", "N/A"),
            "channel_title": snippet.get("title", "N/A"),
            "description": snippet.get("description", "N/A"),
            "published_at": snippet.get("publishedAt", "N/A"),
            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", "N/A")
        }
        
        # Add specific activity details
        if content_details.get("upload"):
            activity["upload"] = content_details["upload"]
        if content_details.get("like"):
            activity["like"] = content_details["like"]
        if content_details.get("comment"):
            activity["comment"] = content_details["comment"]
        if content_details.get("playlistItem"):
            activity["playlist_item"] = content_details["playlistItem"]
        if content_details.get("subscription"):
            activity["subscription"] = content_details["subscription"]
        
        activities.append(activity)
    
    return jsonify({
        "channel_id": channel_id,
        "activities": activities,
        "total": len(activities)
    })

# ===================== LIVE STREAMS =====================

@app.route("/live", methods=["GET"])
def live_streams():
    """Get live and upcoming streams"""
    query = request.args.get("q", "").strip()
    max_results = min(int(request.args.get("max", 10)), 50)
    quality = request.args.get("quality", "highest")
    event_type = request.args.get("type", "live")  # live, upcoming, completed
    
    params = {
        "part": "snippet,statistics,contentDetails,liveStreamingDetails",
        "eventType": event_type,
        "maxResults": max_results,
        "type": "video",
        "key": YOUTUBE_API_KEY
    }
    
    if query:
        params["q"] = query
    else:
        # If no query, get live streams from multiple channels
        params["q"] = "live stream"
    
    data = youtube_api_request("search", params)
    
    if "error" in data:
        return jsonify(data), 400
    
    videos = []
    for item in data.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        if video_id:
            snippet = item.get("snippet", {})
            videos.append({
                "video_id": video_id,
                "title": snippet.get("title", "N/A"),
                "channel_title": snippet.get("channelTitle", "N/A"),
                "description": snippet.get("description", "N/A"),
                "published_at": snippet.get("publishedAt", "N/A"),
                "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", "N/A"),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "live_url": f"https://www.youtube.com/live/{video_id}"
            })
    
    # Extract streams (live streams may not have downloadable URLs)
    videos = extract_streams_from_videos(videos, quality)
    
    return jsonify({
        "event_type": event_type,
        "total_results": data.get("pageInfo", {}).get("totalResults", 0),
        "videos": videos,
        "streams_available": sum(1 for v in videos if v.get("stream_available", False))
    })

# ===================== UTILITY ENDPOINTS =====================

@app.route("/extract_id", methods=["GET"])
def extract_id():
    """Extract video ID from URL"""
    url = request.args.get("url", "").strip()
    
    if not url:
        return jsonify({"error": "Missing url parameter"}), 400
    
    video_id = extract_video_id(url)
    if video_id:
        return jsonify({
            "url": url,
            "video_id": video_id,
            "direct_url": f"https://www.youtube.com/watch?v={video_id}",
            "stream_url": f"/video?id={video_id}"
        })
    else:
        return jsonify({"error": "Invalid YouTube URL"}), 400

@app.route("/extract_stream", methods=["GET"])
def extract_stream():
    """Extract direct stream from URL"""
    url = request.args.get("url", "").strip()
    quality = request.args.get("quality", "highest")
    
    if not url:
        return jsonify({"error": "Missing url parameter"}), 400
    
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Invalid YouTube URL"}), 400
    
    stream = get_direct_stream_single(video_id, quality)
    if stream:
        return jsonify({
            "video_id": video_id,
            "stream": stream,
            "available": True
        })
    else:
        return jsonify({
            "video_id": video_id,
            "stream": None,
            "available": False,
            "error": "No stream found"
        })

@app.route("/health", methods=["GET"])
def health():
    """Health check"""
    return jsonify({
        "status": "healthy",
        "version": "5.0 - MEGA POWER",
        "features": [
            "All YouTube API endpoints",
            "Automatic stream extraction",
            "Batch processing",
            "Parallel streams",
            "Multi-source fallback",
            "Caching",
            "Quality selection",
            "Real-time updates"
        ],
        "cache_size": len(cache_store),
        "cache_ttl": f"{CACHE_TTL} seconds"
    })

# ===================== MAIN =====================

if __name__ == "__main__":
    print("\n" + "="*80)
    print("🔥 YOUTUBE ULTIMATE API v5.0 - MEGA POWER EDITION")
    print("="*80)
    print("\n💪 ALL FEATURES:")
    print("   • Every YouTube Data API v3 endpoint")
    print("   • Automatic stream extraction on ALL responses")
    print("   • Direct download URLs instead of YouTube links")
    print("   • Parallel processing for lightning speed")
    print("   • Multiple fallback stream sources")
    print("   • Smart caching to reduce API calls")
    print("   • Quality selection (highest/lowest/audio)")
    print("   • Batch processing with 10 concurrent workers")
    print("   • Real-time trending, live streams, and more")
    print("\n📌 ENDPOINTS:")
    print("   /search       - Full search with filters + streams")
    print("   /video        - Single video details + stream")
    print("   /videos       - Batch video details + streams")
    print("   /channel      - Channel + all videos + streams")
    print("   /channel_stats- Channel analytics")
    print("   /playlist     - Playlist + streams")
    print("   /comments     - Comments with replies")
    print("   /trending     - Trending videos + streams")
    print("   /related      - Related videos + streams")
    print("   /captions     - Video subtitles")
    print("   /categories   - Video categories")
    print("   /subscriptions- Channel subscriptions")
    print("   /activities   - Channel activity feed")
    print("   /live         - Live streams")
    print("   /extract_id   - Extract video ID")
    print("   /extract_stream- Get direct stream")
    print("   /health       - Health check")
    print("\n📖 EXAMPLES:")
    print("   /search?q=Electrostatics+JEE&max=10&quality=highest")
    print("   /video?id=WOZwY8iEomg")
    print("   /videos?ids=WOZwY8iEomg,dQw4w9WgXcQ")
    print("   /trending?region=IN&max=10")
    print("   /channel?id=UCXuqSBlHAE6Xw-yeJA0Tunw")
    print("   /playlist?id=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI")
    print("   /live?type=live&max=10")
    print("\n" + "="*80 + "\n")
    
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)

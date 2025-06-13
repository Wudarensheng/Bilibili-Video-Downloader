from flask import Flask, render_template, request, Response, redirect, url_for, send_file, jsonify, abort
import hashlib
import re
import requests
import zipfile
import os
from urllib.parse import unquote, quote
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import logging

app = Flask(__name__)

BATCH_PASSWORD_HASH = 'b1e1e7e6e3e2e1e1e7e6e3e2e1e1e7e6e3e2e1e1e7e6e3e2e1e1e7e6e3e2e1e1e7e6e3e2e1e1e7e6e3e2e1e1'  # 占位，后面会替换为正确的sha256

# 全局进度字典
progress_dict = {}
progress_lock = threading.Lock()

def extract_bvid(url):
    # 支持BV号和av号
    bvid_match = re.search(r"BV([\w]+)", url)
    if bvid_match:
        return 'BV' + bvid_match.group(1)
    avid_match = re.search(r"av(\d+)", url)
    if avid_match:
        return int(avid_match.group(1))
    return None

def get_video_url(bili_url, qn=80):
    bvid_or_avid = extract_bvid(bili_url)
    if not bvid_or_avid:
        return None, None, None, None, None, None, None, None, None, "未识别到BV号或av号"
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.bilibili.com/'
    }
    if isinstance(bvid_or_avid, str):
        api_url = f'https://api.bilibili.com/x/web-interface/view?bvid={bvid_or_avid}'
    else:
        api_url = f'https://api.bilibili.com/x/web-interface/view?aid={bvid_or_avid}'
    resp = requests.get(api_url, headers=headers)
    if resp.status_code != 200:
        return None, None, None, None, None, None, None, None, None, "获取视频信息失败"
    data = resp.json()
    if data['code'] != 0:
        return None, None, None, None, None, None, None, None, None, "视频信息解析失败"
    cid = data['data']['cid']
    if isinstance(bvid_or_avid, str):
        bvid = bvid_or_avid
    else:
        bvid = data['data']['bvid']
    title = data['data'].get('title', '')
    desc = data['data'].get('desc', '')
    cover = data['data'].get('pic', '')
    author = data['data'].get('owner', {}).get('name', '')
    # 普通直链
    playurl_api = f'https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn={qn}&fnval=0'
    play_resp = requests.get(playurl_api, headers=headers)
    video_url, m3u8_url, upos_url = None, None, None
    if play_resp.status_code == 200:
        play_data = play_resp.json()
        if play_data['code'] == 0:
            durl = play_data['data'].get('durl', [])
            if durl:
                video_url = durl[0]['url']
                # 提取upos直链
                upos_url = durl[0]['url'] if 'upos' in durl[0]['url'] else None
    # m3u8直链
    playurl_api_m3u8 = f'https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn={qn}&fnval=16'
    play_resp_m3u8 = requests.get(playurl_api_m3u8, headers=headers)
    if play_resp_m3u8.status_code == 200:
        play_data_m3u8 = play_resp_m3u8.json()
        if play_data_m3u8['code'] == 0:
            m3u8_url = play_data_m3u8['data'].get('dash', {}).get('video', [{}])[0].get('baseUrl')
            if not m3u8_url:
                m3u8_url = play_data_m3u8['data'].get('durl', [{}])[0].get('url')
    return video_url, m3u8_url, upos_url, cover, title, desc, author, bvid, None

def get_batch_list(bili_url, qn=80):
    # 只支持多P视频批量解析
    bvid_or_avid = extract_bvid(bili_url)
    if not bvid_or_avid:
        return [], '未识别到BV号或av号'
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.bilibili.com/'
    }
    if isinstance(bvid_or_avid, str):
        api_url = f'https://api.bilibili.com/x/web-interface/view?bvid={bvid_or_avid}'
    else:
        api_url = f'https://api.bilibili.com/x/web-interface/view?aid={bvid_or_avid}'
    resp = requests.get(api_url, headers=headers)
    if resp.status_code != 200:
        return [], '获取视频信息失败'
    data = resp.json()
    if data['code'] != 0:
        return [], '视频信息解析失败'
    pages = data['data'].get('pages', [])
    bvid = data['data'].get('bvid', '')
    batch_list = []
    for p in pages:
        cid = p.get('cid')
        title = p.get('part')
        page = p.get('page')
        # 获取该分P的直链
        video_url, m3u8_url, upos_url = get_video_url_by_bvid_cid(bvid, cid, qn)
        batch_list.append({
            'bvid': bvid,
            'cid': cid,
            'title': title,
            'page': page,
            'video_url': video_url,
            'm3u8_url': m3u8_url,
            'upos_url': upos_url
        })
    if not pages:
        cid = data['data'].get('cid')
        title = data['data'].get('title')
        video_url, m3u8_url, upos_url = get_video_url_by_bvid_cid(bvid, cid, qn)
        batch_list.append({
            'bvid': bvid,
            'cid': cid,
            'title': title,
            'page': 1,
            'video_url': video_url,
            'm3u8_url': m3u8_url,
            'upos_url': upos_url
        })
    return batch_list, None

def get_video_url_by_bvid_cid(bvid, cid, qn=80):
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.bilibili.com/'
    }
    playurl_api = f'https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn={qn}&fnval=0'
    play_resp = requests.get(playurl_api, headers=headers)
    video_url, m3u8_url, upos_url = None, None, None
    if play_resp.status_code == 200:
        play_data = play_resp.json()
        if play_data['code'] == 0:
            durl = play_data['data'].get('durl', [])
            if durl:
                video_url = durl[0]['url']
                upos_url = durl[0]['url'] if 'upos' in durl[0]['url'] else None
    # m3u8直链
    playurl_api_m3u8 = f'https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn={qn}&fnval=16'
    play_resp_m3u8 = requests.get(playurl_api_m3u8, headers=headers)
    if play_resp_m3u8.status_code == 200:
        play_data_m3u8 = play_resp_m3u8.json()
        if play_data_m3u8['code'] == 0:
            m3u8_url = play_data_m3u8['data'].get('dash', {}).get('video', [{}])[0].get('baseUrl')
            if not m3u8_url:
                m3u8_url = play_data_m3u8['data'].get('durl', [{}])[0].get('url')
    return video_url, m3u8_url, upos_url

@app.errorhandler(Exception)
def handle_exception(e):
    logging.exception(e)
    return render_template('error.html', error=str(e)), 500

# 检查视频直链有效性
def check_url_valid(url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://www.bilibili.com/'
        }
        resp = requests.head(url, headers=headers, timeout=8, allow_redirects=True)
        if resp.status_code == 200:
            return True
        return False
    except Exception:
        return False

@app.route('/', methods=['GET', 'POST'])
def index():
    video_url = None
    m3u8_url = None
    upos_url = None
    error = None
    cover = None
    title = None
    desc = None
    author = None
    bvid = None
    qn = 80
    qn_map = {'360p': 16, '480p': 32, '720p': 64, '1080p': 80}
    selected = '1080p'
    batch = False
    batch_list = None
    batch_error = None
    if request.method == 'POST' and request.path == '/':
        bili_url = request.form.get('bili_url')
        selected = request.form.get('qn', '1080p')
        qn = qn_map.get(selected, 80)
        video_url, m3u8_url, upos_url, cover, title, desc, author, bvid, error = get_video_url(bili_url, qn)
    return render_template('index.html', video_url=video_url, m3u8_url=m3u8_url, upos_url=upos_url, error=error, selected=selected, cover=cover, title=title, desc=desc, author=author, bvid=bvid, batch=batch, batch_list=batch_list, batch_error=batch_error)

@app.route('/batch', methods=['GET', 'POST'])
def batch_route():
    batch_list = []
    batch_error = None
    selected = '1080p'
    batch = True
    qn_map = {'360p': 16, '480p': 32, '720p': 64, '1080p': 80}
    if request.method == 'POST':
        selected = request.form.get('qn', '1080p')
        bili_urls = request.form.get('bili_urls', '')
        all_links = [line.strip() for line in bili_urls.splitlines() if line.strip()]
        qn = qn_map.get(selected, 80)
        if len(all_links) == 1:
            blist, err = get_batch_list(all_links[0], qn)
            if err or not blist:
                batch_error = err or '未能解析到分P'
                return render_template('batch.html', batch_list=None, batch_error=batch_error, selected=selected, batch=batch)
            bvid = blist[0]['bvid']
            cid = blist[0]['cid']
            title = blist[0]['title']
            video_url = blist[0]['video_url']
            if not video_url:
                batch_error = '获取视频直链失败'
                return render_template('batch.html', batch_list=None, batch_error=batch_error, selected=selected, batch=batch)
            from urllib.parse import quote as urlquote
            return redirect(url_for('download', url=urlquote(video_url), title=title))
        for link in all_links:
            blist, err = get_batch_list(link, qn)
            if err:
                if not batch_error:
                    batch_error = err + f'：{link}'
                continue
            batch_list.extend(blist)
        if not batch_list and not batch_error:
            batch_error = '未能解析到任何分P，请检查链接是否正确'
    else:
        batch_list = None
    return render_template('batch.html', batch_list=batch_list, batch_error=batch_error, selected=selected, batch=batch)

@app.route('/batch_download', methods=['POST'])
def batch_download():
    import uuid
    import datetime
    selected_parts = request.form.getlist('selected_parts')
    qn_map = {'360p': 16, '480p': 32, '720p': 64, '1080p': 80}
    selected = request.form.get('qn', '1080p')
    qn = qn_map.get(selected, 80)
    files = []
    task_ids = []
    for part in selected_parts:
        try:
            bvid, cid, title, page = part.split('|', 3)
            task_id = str(uuid.uuid4())
            files.append((f"P{page}_{title}", bvid, cid, task_id))
            task_ids.append(task_id)
        except Exception:
            continue

    # 创建TEMP/日期文件夹
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'TEMP', today_str)
    try:
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
    except Exception:
        pass  # 文件夹已存在或创建失败都不影响后续
    temp_files = []

    def download_one(title, bvid, cid, qn, task_id):
        try:
            video_url, m3u8_url, upos_url = get_video_url_by_bvid_cid(bvid, cid, qn)
            if not video_url:
                return None
            safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:80]
            filename = f"{safe_title}.mp4"
            temp_path = os.path.join(temp_dir, filename)
            temp_path_temp = temp_path + '.nebulatemp'
            # 如果已存在完整mp4文件，直接返回
            if os.path.exists(temp_path):
                with progress_lock:
                    progress_dict[task_id] = {'percent': 100, 'finished': True}
                return temp_path, filename, task_id
            # 如果存在未完成的缓存文件，断点续传（简单实现：继续写入）
            mode = 'ab' if os.path.exists(temp_path_temp) else 'wb'
            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://www.bilibili.com/'
            }
            r = requests.get(video_url, stream=True, headers=headers, timeout=15)
            total = int(r.headers.get('content-length', 0))
            downloaded = os.path.getsize(temp_path_temp) if os.path.exists(temp_path_temp) else 0
            with open(temp_path_temp, mode) as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        percent = int(downloaded * 100 / total) if total else 0
                        with progress_lock:
                            progress_dict[task_id] = {'percent': percent, 'finished': False}
            os.replace(temp_path_temp, temp_path)
            with progress_lock:
                progress_dict[task_id] = {'percent': 100, 'finished': True}
            return temp_path, filename, task_id
        except Exception as e:
            with progress_lock:
                progress_dict[task_id] = {'percent': 0, 'finished': True, 'error': str(e)}
            return None

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_title = {executor.submit(download_one, title, bvid, cid, qn, task_id): (title, task_id) for title, bvid, cid, task_id in files}
        for future in as_completed(future_to_title):
            result = future.result()
            if result:
                temp_files.append(result)

    # 打包为zip
    mem_zip = BytesIO()
    # 返回task_id映射，前端可用
    taskid_map = {f'{bvid}_{cid}': task_id for (title, bvid, cid, task_id) in files}
    try:
        with zipfile.ZipFile(mem_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
            for temp_path, filename, task_id in temp_files:
                try:
                    if temp_path and os.path.exists(temp_path):
                        with open(temp_path, 'rb') as f:
                            zf.writestr(filename, f.read())
                except Exception:
                    continue
        mem_zip.seek(0)
        resp = send_file(mem_zip, mimetype='application/zip', as_attachment=True, download_name='bilibili_batch.zip')
        # 在响应头中返回taskid_map，前端可解析
        resp.headers['X-Taskid-Map'] = str(taskid_map)
        return resp
    except Exception as e:
        return '打包下载失败，请重试', 500

@app.route('/progress/<task_id>')
def get_progress(task_id):
    with progress_lock:
        prog = progress_dict.get(task_id, {'percent': 0, 'finished': False})
    return jsonify(prog)

@app.route('/download')
def download():
    url = request.args.get('url')
    title = request.args.get('title', 'video')
    if not url:
        return '未提供视频直链', 400
    # 检查直链有效性
    if not check_url_valid(url):
        return '视频直链已失效或无法访问，请重新解析', 410
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.bilibili.com/'
    }
    range_header = request.headers.get('Range', None)
    r = requests.get(url, stream=True, headers=headers)
    def generate():
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                yield chunk
    response_headers = {
        'Content-Disposition': f'attachment; filename="{title}.mp4"',
        'Content-Type': 'video/mp4'
    }
    # 支持断点续传
    if range_header:
        response_headers['Accept-Ranges'] = 'bytes'
    return Response(generate(), headers=response_headers)

@app.route('/proxy')
def proxy():
    url = request.args.get('url')
    if not url:
        return '未提供视频直链', 400
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.bilibili.com/'
    }
    r = requests.get(url, stream=True, headers=headers)
    def generate():
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                yield chunk
    resp = Response(generate(), content_type='video/mp4')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route('/play')
def play():
    url = request.args.get('url')
    title = request.args.get('title', 'video')
    cover = request.args.get('cover', '')
    if not url:
        return '未提供视频直链', 400
    # 预览时用代理，防止直链被拦截
    proxy_url = url_for('proxy', url=url)
    return render_template('play.html', url=proxy_url, title=title, cover=cover)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)


// ==UserScript==
// @name         YouTube Downloader — видео и обложки
// @namespace    https://github.com/silenttilt/gptytdownloader
// @version      1.0.0
// @description  Видео через собственный локальный yt-dlp API; обложки напрямую с YouTube.
// @match        https://www.youtube.com/*
// @match        https://youtube.com/*
// @match        https://m.youtube.com/*
// @run-at       document-idle
// @grant        GM_download
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @connect      127.0.0.1
// @connect      localhost
// @connect      i.ytimg.com
// ==/UserScript==

(() => {
  'use strict';

  const API_BASE = 'http://127.0.0.1:8765';
  const HOST_ID = 'ytdl-tm-host';
  const VIDEO_ID_PATTERN = /^[A-Za-z0-9_-]{11}$/;
  const COVER_CANDIDATES = Object.freeze([
    { key: 'maxresdefault', label: '1280 × 720', width: 1280, height: 720 },
    { key: 'sddefault', label: '640 × 480', width: 640, height: 480 },
    { key: 'hqdefault', label: '480 × 360', width: 480, height: 360 },
    { key: 'mqdefault', label: '320 × 180', width: 320, height: 180 },
  ]);

  function parseVideoId(address) {
    try {
      const url = new URL(address);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password
        || !['www.youtube.com', 'youtube.com', 'm.youtube.com'].includes(url.hostname)) return null;
      if (url.pathname === '/watch' && url.searchParams.getAll('v').length !== 1) return null;
      const id = url.pathname === '/watch' ? url.searchParams.get('v') : /^\/shorts\/([^/]+)\/?$/.exec(url.pathname)?.[1];
      return id && VIDEO_ID_PATTERN.test(id) ? id : null;
    } catch {
      return null;
    }
  }

  function canonicalVideoUrl(id) {
    if (!VIDEO_ID_PATTERN.test(id)) throw new Error('Некорректный идентификатор видео.');
    return `https://www.youtube.com/watch?v=${id}`;
  }

  function sanitizeFilename(title, fallback = 'YouTube') {
    let safe = String(title || fallback).normalize('NFC')
      .replace(/[<>:"/\\|?*\u0000-\u001f\u007f-\u009f]/g, '_').replace(/\p{Cf}/gu, '')
      .replace(/\s+/g, ' ').trim().replace(/[. ]+$/g, '');
    if (!safe) safe = fallback;
    if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(safe)) safe = `_${safe}`;
    const encoder = new TextEncoder();
    let result = '';
    let byteLength = 0;
    for (const character of safe) {
      const size = encoder.encode(character).length;
      if (byteLength + size > 200) break;
      result += character;
      byteLength += size;
    }
    return result.replace(/[. ]+$/g, '') || fallback;
  }

  function validThumbnailDimensions(candidate, width, height) {
    if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 120 || height <= 90) return false;
    if (candidate.key === 'maxresdefault') return width >= candidate.width && height >= candidate.height;
    return width === candidate.width && height === candidate.height;
  }

  function normalizeProgress(value) {
    return typeof value === 'number' && Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : null;
  }

  // The same pure functions are exercised by Node tests; no browser globals are
  // accessed when this file is loaded as a CommonJS module.
  if (typeof module === 'object' && module.exports) {
    module.exports = { parseVideoId, canonicalVideoUrl, sanitizeFilename, validThumbnailDimensions, normalizeProgress, COVER_CANDIDATES };
    return;
  }
  if (typeof document === 'undefined' || document.getElementById(HOST_ID)) return;

  const storedValue = (key, fallback) => {
    try { return GM_getValue(key, fallback); } catch { return fallback; }
  };
  const rememberValue = (key, value) => {
    try { GM_setValue(key, value); } catch { /* Settings can still be used in this tab. */ }
  };
  const initialTab = storedValue('preferredTab', 'video');
  const state = {
    video: null,
    revision: 0,
    opened: false,
    tab: initialTab === 'cover' ? 'cover' : 'video',
    token: String(storedValue('apiToken', '') || '').trim(),
    formats: null,
    formatsLoading: false,
    formatsError: '',
    formatsGeneration: 0,
    covers: [],
    coversLoading: false,
    coversLoaded: false,
    coverError: '',
    coverDownloading: false,
    starting: false,
    activeTask: null,
    pollTimer: null,
    pollFailures: 0,
    pollBusy: false,
    pollError: '',
  };
  const requests = new Set();
  const svgNS = 'http://www.w3.org/2000/svg';

  function el(tag, attributes = {}, content = null) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes)) {
      if (name === 'class') node.className = value;
      else node.setAttribute(name, String(value));
    }
    if (content !== null) node.textContent = String(content);
    return node;
  }

  function downloadIcon() {
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(svgNS, 'path');
    path.setAttribute('d', 'M11 3h2v10l3.5-3.5 1.4 1.4L12 17l-5.9-6.1 1.4-1.4L11 13V3ZM5 18h14v3H5v-3Z');
    svg.append(path);
    return svg;
  }

  const host = el('div', { id: HOST_ID });
  host.style.cssText = 'display:inline-flex;flex:0 0 auto;align-items:center;margin:0 0 0 8px;z-index:2147483646;';
  // The page should not be able to inspect a token while the user types it.
  const shadow = host.attachShadow({ mode: 'closed' });
  const style = el('style');
  style.textContent = `
    :host { color-scheme: light; --bg:#fff; --fg:#0f0f0f; --muted:#606060; --line:#dedede; --soft:#f2f2f2; --hover:#e5e5e5; --accent:#065fd4; --accent-bg:#def1ff; --error:#b3261e; font:14px/1.45 Arial, sans-serif; color:var(--fg); }
    :host([data-dark]) { color-scheme:dark; --bg:#212121; --fg:#f1f1f1; --muted:#aaa; --line:#424242; --soft:#363636; --hover:#484848; --accent:#8ab4f8; --accent-bg:#263d52; --error:#ffb4ab; }
    *, *::before, *::after { box-sizing:border-box; }
    [hidden] { display:none !important; }
    button,input,select { font:inherit; }
    button { cursor:pointer; border:0; color:var(--fg); transition:background .16s, opacity .16s; }
    button:disabled { cursor:default; opacity:.48; }
    button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible { outline:2px solid var(--accent); outline-offset:3px; }
    button svg { width:20px; height:20px; fill:currentColor; flex:0 0 auto; }
    #download-button { display:inline-flex; align-items:center; gap:8px; height:36px; padding:0 15px; border-radius:20px; background:var(--soft); white-space:nowrap; font-weight:600; }
    #download-button:hover { background:var(--hover); }
    #download-panel { position:fixed; z-index:2147483647; width:380px; max-width:calc(100vw - 24px); max-height:calc(100vh - 24px); overflow:auto; background:var(--bg); border:1px solid var(--line); border-radius:16px; box-shadow:0 8px 32px #0003; animation:appear .14s ease-out; padding:16px; color:var(--fg); }
    @keyframes appear { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:translateY(0); } }
    @media (prefers-reduced-motion:reduce) { *, *::before, *::after { animation:none !important; transition:none !important; } }
    .panel-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; }
    .panel-head strong { font-size:16px; }
    #close-panel { border-radius:50%; width:30px; height:30px; background:var(--soft); font-size:23px; line-height:1; }
    .tabs { display:flex; padding:3px; border-radius:12px; gap:4px; background:var(--soft); margin-bottom:14px; }
    .tabs button { flex:1; padding:8px; border-radius:9px; background:transparent; }
    .tabs button[aria-selected="true"] { background:var(--bg); font-weight:600; box-shadow:0 1px 3px #0002; }
    .video-card { display:flex; gap:10px; margin-bottom:12px; align-items:center; }
    #video-preview { width:96px; height:54px; border-radius:8px; object-fit:cover; background:var(--soft); }
    #video-title { font-size:13px; line-height:1.4; overflow:hidden; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow-wrap:anywhere; }
    .hint { color:var(--muted); font-size:12px; margin:8px 0; overflow-wrap:anywhere; }
    .status { margin:8px 0; font-size:13px; overflow-wrap:anywhere; }
    .error { color:var(--error); }
    label { display:block; font-size:12px; color:var(--muted); margin-bottom:6px; }
    input,select { width:100%; border:1px solid var(--line); background:var(--bg); color:var(--fg); border-radius:9px; padding:9px 10px; min-width:0; }
    .primary { display:flex; align-items:center; justify-content:center; gap:8px; width:100%; margin-top:12px; padding:10px 12px; border-radius:10px; color:var(--accent); background:var(--accent-bg); font-weight:600; }
    .primary:hover:enabled { filter:brightness(.97); }
    .secondary { background:var(--soft); border-radius:8px; padding:8px 10px; }
    details { margin-top:14px; border-top:1px solid var(--line); padding-top:10px; }
    summary { color:var(--muted); font-size:12px; cursor:pointer; }
    details label { margin-top:10px; }
    #save-token { margin-top:8px; }
    code { display:block; margin:6px 0; font:12px/1.4 ui-monospace,monospace; color:var(--fg); user-select:all; }
    #task-card { margin-top:14px; padding-top:12px; border-top:1px solid var(--line); }
    #task-title { font-size:12px; color:var(--muted); overflow-wrap:anywhere; }
    #task-progress { width:100%; height:7px; border:none; border-radius:4px; accent-color:var(--accent); margin:7px 0; }
    #task-filename { font-size:12px; color:var(--muted); overflow-wrap:anywhere; }
    #resume-task { margin-top:8px; }
    #cover-preview { display:block; width:100%; max-height:210px; object-fit:contain; border-radius:10px; margin:10px 0; background:var(--soft); }
    #cover-manual-link { display:inline-block; font-size:12px; color:var(--accent); margin-top:8px; }
    @media (max-width:480px) { #download-panel { padding:13px; } #download-button { padding:0 12px; } }
  `;
  const button = el('button', { id: 'download-button', type: 'button', 'aria-haspopup': 'dialog', 'aria-expanded': 'false', 'aria-controls': 'download-panel' });
  button.append(downloadIcon(), el('span', {}, 'Скачать'));
  const panel = el('section', { id: 'download-panel', role: 'dialog', 'aria-label': 'Скачивание видео и обложки' });
  panel.hidden = true;
  const heading = el('div', { class: 'panel-head' });
  const closeButton = el('button', { id: 'close-panel', type: 'button', 'aria-label': 'Закрыть' }, '×');
  heading.append(el('strong', {}, 'YouTube Downloader'), closeButton);
  const tabs = el('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Что скачать' });
  const videoTab = el('button', { id: 'video-tab', type: 'button', role: 'tab', 'aria-controls': 'video-view' }, 'Видео');
  const coverTab = el('button', { id: 'cover-tab', type: 'button', role: 'tab', 'aria-controls': 'cover-view' }, 'Обложка');
  tabs.append(videoTab, coverTab);

  const videoView = el('div', { id: 'video-view', role: 'tabpanel', 'aria-labelledby': 'video-tab' });
  const videoCard = el('div', { class: 'video-card' });
  const videoPreview = el('img', { id: 'video-preview', alt: '', referrerpolicy: 'no-referrer' });
  const videoTitle = el('div', { id: 'video-title' });
  videoCard.append(videoPreview, videoTitle);
  const videoStatus = el('div', { id: 'video-status', class: 'status', role: 'status', 'aria-live': 'polite' });
  const qualitySelect = el('select', { id: 'video-quality' });
  const startButton = el('button', { id: 'start-download', type: 'button', class: 'primary' });
  startButton.append(downloadIcon(), el('span', {}, 'Скачать видео'));
  const formatHint = el('p', { class: 'hint' });
  const refreshButton = el('button', { id: 'refresh-formats', type: 'button', class: 'secondary' }, 'Проверить снова');
  const settings = el('details', { id: 'server-settings' });
  settings.open = !state.token;
  const tokenInput = el('input', { id: 'api-token', type: 'password', autocomplete: 'off', spellcheck: 'false', placeholder: 'Токен из терминала Python-сервера' });
  tokenInput.placeholder = state.token ? 'Токен сохранён. Введите новый для замены.' : 'Токен из терминала Python-сервера';
  const saveToken = el('button', { id: 'save-token', type: 'button', class: 'secondary' }, 'Сохранить токен');
  const setupHint = el('p', { class: 'hint' }, 'В папке youtube-downloader активируйте своё .venv. Первая команда покажет токен: скопируйте его в поле выше. Вторая запустит сервер.');
  setupHint.append(el('code', {}, 'python backend/server.py --show-token'), el('code', {}, 'python backend/server.py'));
  settings.append(el('summary', {}, 'Подключение к локальному серверу'), el('label', { for: 'api-token' }, 'Токен доступа'), tokenInput, saveToken, setupHint, el('p', { class: 'hint' }, `Адрес: ${API_BASE}. Токен хранится только в настройках Tampermonkey.`));
  videoView.append(videoCard, videoStatus, el('label', { for: 'video-quality' }, 'Качество и контейнер'), qualitySelect, formatHint, startButton, refreshButton, settings);

  const coverView = el('div', { id: 'cover-view', role: 'tabpanel', 'aria-labelledby': 'cover-tab' });
  const coverStatus = el('div', { id: 'cover-status', class: 'status', role: 'status', 'aria-live': 'polite' });
  const coverSelect = el('select', { id: 'cover-quality' });
  const coverPreview = el('img', { id: 'cover-preview', alt: 'Предварительный просмотр обложки' });
  const coverButton = el('button', { id: 'download-cover', type: 'button', class: 'primary' });
  coverButton.append(downloadIcon(), el('span', {}, 'Скачать обложку'));
  const coverManualLink = el('a', { id: 'cover-manual-link', target: '_blank', rel: 'noopener noreferrer' }, 'Открыть изображение для сохранения вручную');
  coverManualLink.hidden = true;
  coverView.append(el('p', { class: 'hint' }, 'Обложки скачиваются напрямую с YouTube. Python-сервер не требуется.'), coverStatus, el('label', { for: 'cover-quality' }, 'Разрешение обложки'), coverSelect, coverPreview, coverButton, coverManualLink);

  const taskCard = el('div', { id: 'task-card', role: 'status', 'aria-live': 'polite' });
  taskCard.hidden = true;
  const taskTitle = el('div', { id: 'task-title' });
  const taskStatus = el('div', { id: 'task-status', class: 'status' });
  const taskProgress = el('progress', { id: 'task-progress', max: '100', 'aria-label': 'Прогресс загрузки' });
  const taskFilename = el('div', { id: 'task-filename' });
  const resumeTask = el('button', { id: 'resume-task', type: 'button', class: 'secondary' }, 'Восстановить связь');
  taskCard.append(taskTitle, taskStatus, taskProgress, taskFilename, resumeTask);
  panel.append(heading, tabs, videoView, coverView, taskCard);
  shadow.append(style, button, panel);

  function request(options, scope = '') {
    return new Promise((resolve, reject) => {
      const record = { handle: null, scope };
      requests.add(record);
      const finish = (callback, value) => {
        requests.delete(record);
        callback(value);
      };
      try {
        record.handle = GM_xmlhttpRequest({
          timeout: 15000,
          ...options,
          onload: (response) => finish(resolve, response),
          onerror: () => finish(reject, new Error(scope === 'cover'
            ? 'Не удалось получить обложку с i.ytimg.com. Проверьте соединение и разрешения Tampermonkey на доступ к этому адресу.'
            : 'Нет связи с сервером. Проверьте запуск Python-сервера и разрешения Tampermonkey на доступ к 127.0.0.1.')),
          ontimeout: () => finish(reject, new Error('Истекло время ожидания ответа. Проверьте соединение и повторите попытку.')),
          onabort: () => {
            const error = new Error('Запрос отменён после перехода на другую страницу.');
            error.name = 'AbortError';
            finish(reject, error);
          },
        });
      } catch (error) {
        finish(reject, error);
      }
    });
  }

  function cancelPageRequests(scopes = ['metadata', 'cover']) {
    for (const record of [...requests]) {
      if (scopes.includes(record.scope)) {
        try { record.handle?.abort(); } catch { /* A completed request needs no cancellation. */ }
      }
    }
  }

  async function api(path, { method = 'GET', data, scope = '', timeout = 15000 } = {}) {
    if (!state.token) throw new Error('Введите токен доступа из терминала Python-сервера.');
    const response = await request({
      method,
      timeout,
      url: `${API_BASE}${path}`,
      anonymous: true,
      headers: { 'X-YTD-Token': state.token, ...(data ? { 'Content-Type': 'application/json' } : {}) },
      ...(data ? { data: JSON.stringify(data) } : {}),
    }, scope);
    let body;
    try { body = JSON.parse(response.responseText); } catch { throw new Error('Локальный сервер вернул некорректный ответ. Проверьте, что на порту 8765 запущен backend этого проекта.'); }
    if (response.status < 200 || response.status >= 300) {
      const error = new Error(body.error || `Ошибка HTTP ${response.status}.`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function busy() {
    return state.starting || (state.activeTask && !['completed', 'error'].includes(state.activeTask.status));
  }

  function currentRevision(revision) {
    return state.revision === revision && Boolean(state.video);
  }

  function renderVideo() {
    videoTitle.textContent = state.video?.title || 'YouTube';
    const previewURL = state.video ? `https://i.ytimg.com/vi/${state.video.id}/hqdefault.jpg` : '';
    if (videoPreview.getAttribute('src') !== previewURL) {
      if (previewURL) videoPreview.src = previewURL;
      else videoPreview.removeAttribute('src');
    }
    videoStatus.classList.toggle('error', Boolean(state.formatsError));
    videoStatus.textContent = state.formatsError || (state.formatsLoading ? 'Подготовка: проверяем сервер и доступные форматы…' : state.formats ? 'Выберите доступное качество.' : state.token ? 'Нажмите «Проверить снова» для подключения.' : 'Для видео запустите локальный сервер и сохраните его токен.');
    const oldQuality = qualitySelect.value || String(storedValue('preferredQuality', 'best'));
    qualitySelect.replaceChildren();
    const qualities = state.formats?.qualities || [];
    for (const quality of qualities) {
      const container = quality.container ? String(quality.container).toUpperCase() : 'контейнер определяется сервером';
      const option = el('option', { value: quality.value }, `${quality.label || quality.value} · ${container}${quality.requires_ffmpeg && !state.formats.ffmpeg_available ? ' · нужен FFmpeg' : ''}`);
      option.disabled = Boolean(quality.requires_ffmpeg && !state.formats.ffmpeg_available);
      qualitySelect.append(option);
    }
    if (!qualities.length) qualitySelect.append(el('option', { value: '' }, 'Список качеств пока недоступен'));
    const matching = [...qualitySelect.options].find((option) => option.value === oldQuality && !option.disabled);
    const fallback = [...qualitySelect.options].find((option) => !option.disabled);
    qualitySelect.value = matching?.value || fallback?.value || '';
    qualitySelect.disabled = !qualities.length || state.formatsLoading || Boolean(busy());
    startButton.disabled = !state.video || !qualitySelect.value || Boolean(busy()) || state.formatsLoading;
    refreshButton.hidden = false;
    refreshButton.textContent = state.formats ? 'Обновить качества' : 'Проверить снова';
    refreshButton.disabled = state.formatsLoading;
    formatHint.textContent = state.formats ? (state.formats.ffmpeg_available ? 'MP4 с аудио выбирается при совместимых дорожках. Если нужен другой контейнер, он указан в списке. Файл сохранит локальный сервер.' : 'FFmpeg не найден: доступны только готовые файлы с видео и аудио. Установите FFmpeg для остальных качеств.') : '';
  }

  async function ensureFormats(force = false) {
    if (!state.video || state.formatsLoading || (!force && state.formats)) return;
    if (!state.token) {
      state.formatsError = '';
      renderVideo();
      return;
    }
    const revision = state.revision;
    const generation = state.formatsGeneration;
    const currentRequest = () => currentRevision(revision) && state.formatsGeneration === generation;
    const url = state.video.url;
    state.formatsLoading = true;
    state.formatsError = '';
    renderVideo();
    try {
      const health = await api('/api/health', { scope: 'metadata', timeout: 5000 });
      if (!health.ok) throw new Error('Локальный сервер ещё не готов.');
      if (!currentRequest()) return;
      const formats = await api(`/api/formats?url=${encodeURIComponent(url)}`, { scope: 'metadata', timeout: 90000 });
      if (!currentRequest()) return;
      if (!Array.isArray(formats.qualities) || formats.qualities.length === 0) throw new Error('Нет подходящих форматов с аудио. Установите FFmpeg или проверьте доступность видео.');
      state.formats = formats;
      if (formats.title) state.video.title = String(formats.title);
    } catch (error) {
      if (currentRequest() && error.name !== 'AbortError') {
        state.formats = null;
        state.formatsError = error.message;
        if (error.status === 401) settings.open = true;
      }
    } finally {
      if (currentRequest()) {
        state.formatsLoading = false;
        renderVideo();
        positionPanel();
      }
    }
  }

  function clearCovers() {
    for (const cover of state.covers) URL.revokeObjectURL(cover.blobURL);
    state.covers = [];
    state.coversLoaded = false;
    state.coversLoading = false;
    state.coverError = '';
    coverPreview.removeAttribute('src');
    coverManualLink.hidden = true;
  }

  async function probeCover(id, candidate) {
    const url = `https://i.ytimg.com/vi/${id}/${candidate.key}.jpg`;
    const response = await request({ method: 'GET', url, responseType: 'blob', anonymous: true }, 'cover');
    if (response.status !== 200) return null;
    const received = response.response;
    // Firefox can return a Blob from the extension's realm rather than this
    // window, so instanceof alone is not a portable identity check.
    if (Object.prototype.toString.call(received) !== '[object Blob]' || received.size === 0) return null;
    const blob = received instanceof Blob ? received : new Blob([received], { type: received.type || 'image/jpeg' });
    const blobURL = URL.createObjectURL(blob);
    try {
      const dimensions = await new Promise((resolve, reject) => {
        const image = new Image();
        const timer = window.setTimeout(() => {
          image.onload = image.onerror = null;
          reject(new Error('Не удалось прочитать изображение.'));
        }, 8000);
        image.onload = () => {
          window.clearTimeout(timer);
          resolve({ width: image.naturalWidth, height: image.naturalHeight });
        };
        image.onerror = () => {
          window.clearTimeout(timer);
          reject(new Error('Не удалось прочитать изображение.'));
        };
        image.src = blobURL;
      });
      if (!validThumbnailDimensions(candidate, dimensions.width, dimensions.height)) {
        URL.revokeObjectURL(blobURL);
        return null;
      }
      return { ...candidate, ...dimensions, url, blobURL, blob };
    } catch (error) {
      URL.revokeObjectURL(blobURL);
      throw error;
    }
  }

  function selectedCover() {
    if (coverSelect.value === 'best') return state.covers[0] || null;
    return state.covers.find((cover) => cover.key === coverSelect.value) || state.covers[0] || null;
  }

  function updateCoverPreview() {
    const cover = selectedCover();
    coverPreview.hidden = !cover;
    if (cover) {
      coverPreview.src = cover.blobURL;
      coverPreview.alt = `Обложка: ${state.video?.title || 'YouTube'}, ${cover.width} × ${cover.height}`;
      coverManualLink.href = cover.url;
    } else {
      coverPreview.removeAttribute('src');
      coverManualLink.removeAttribute('href');
    }
    coverButton.disabled = !cover || state.coverDownloading;
  }

  function renderCovers() {
    coverStatus.classList.toggle('error', Boolean(state.coverError));
    coverStatus.textContent = state.coverError || (state.coversLoading ? 'Проверяем доступные изображения и их разрешение…' : state.covers.length ? 'Доступны только проверенные изображения, без заглушек 120 × 90.' : state.coversLoaded ? 'Не удалось найти доступную обложку.' : 'Откройте вкладку для проверки обложек.');
    const previous = coverSelect.value;
    coverSelect.replaceChildren();
    if (state.covers.length) {
      const best = state.covers[0];
      coverSelect.append(el('option', { value: 'best' }, `Максимальное доступное · ${best.width} × ${best.height}`));
      for (const cover of state.covers) coverSelect.append(el('option', { value: cover.key }, `${cover.width} × ${cover.height}`));
      coverSelect.value = [...coverSelect.options].some((option) => option.value === previous) ? previous : 'best';
    } else {
      coverSelect.append(el('option', { value: '' }, 'Обложки пока недоступны'));
    }
    coverSelect.disabled = !state.covers.length || state.coverDownloading;
    updateCoverPreview();
  }

  async function ensureCovers() {
    if (!state.video || state.coversLoading || state.coversLoaded) return;
    const revision = state.revision;
    const id = state.video.id;
    state.coversLoading = true;
    renderCovers();
    const results = await Promise.allSettled(COVER_CANDIDATES.map((candidate) => probeCover(id, candidate)));
    const covers = results.filter((result) => result.status === 'fulfilled' && result.value).map((result) => result.value)
      .sort((a, b) => b.width * b.height - a.width * a.height);
    if (!currentRevision(revision)) {
      covers.forEach((cover) => URL.revokeObjectURL(cover.blobURL));
      return;
    }
    state.covers = covers;
    state.coversLoading = false;
    state.coversLoaded = true;
    if (!covers.length) state.coverError = 'YouTube не предоставил подходящую обложку. Проверьте соединение и разрешение Tampermonkey на доступ к i.ytimg.com.';
    renderCovers();
    positionPanel();
  }

  function gmDownload(url, filename) {
    return new Promise((resolve, reject) => {
      try {
        GM_download({
          url,
          name: filename,
          saveAs: true,
          timeout: 30000,
          onload: resolve,
          onerror: () => reject(new Error('Браузер не разрешил скачивание через Tampermonkey.')),
          ontimeout: () => reject(new Error('Истекло время ожидания скачивания обложки.')),
        });
      } catch (error) { reject(error); }
    });
  }

  async function downloadCover() {
    const cover = selectedCover();
    if (!cover || !state.video || state.coverDownloading) return;
    const revision = state.revision;
    const filename = `${sanitizeFilename(state.video.title, `YouTube_${state.video.id}`)}_thumbnail.jpg`;
    state.coverDownloading = true;
    coverStatus.classList.remove('error');
    coverStatus.textContent = 'Скачивание обложки…';
    coverButton.disabled = true;
    coverSelect.disabled = true;
    try {
      await gmDownload(cover.url, filename);
      if (currentRevision(revision)) coverStatus.textContent = 'Готово: обложка скачана.';
    } catch {
      // The image has already been fetched with GM_xmlhttpRequest and verified.
      // A separate blob URL stays alive long enough for slow browser download UI.
      const fallbackURL = URL.createObjectURL(cover.blob);
      const link = el('a', { href: fallbackURL, download: filename });
      link.style.display = 'none';
      shadow.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(fallbackURL), 60000);
      if (currentRevision(revision)) {
        coverStatus.textContent = 'Запрошено сохранение через браузер. Если оно не началось, откройте изображение по ссылке ниже и сохраните вручную.';
        coverManualLink.hidden = false;
      }
    } finally {
      state.coverDownloading = false;
      if (currentRevision(revision)) {
        coverSelect.disabled = false;
        coverButton.disabled = false;
      } else if (state.video) {
        renderCovers();
      }
    }
  }

  function renderTask() {
    const task = state.activeTask;
    taskCard.hidden = !task && !state.starting;
    if (!task) return;
    taskTitle.textContent = `Задача: ${task.videoTitle || task.title || 'YouTube'}`;
    const labels = { preparing: 'Подготовка…', downloading: 'Скачивание…', processing: 'Обработка: объединяем видео и аудио…', completed: 'Завершено: файл сохранён локальным сервером.', error: 'Ошибка загрузки.' };
    taskStatus.classList.toggle('error', task.status === 'error' || Boolean(state.pollError));
    const progress = normalizeProgress(task.progress);
    taskStatus.textContent = state.pollError || (task.status === 'error' ? `Ошибка: ${task.error || 'Не удалось скачать видео.'}` : `${labels[task.status] || 'Подготовка…'}${task.status === 'downloading' && progress !== null ? ` ${Math.round(progress)}%` : ''}`);
    taskProgress.hidden = ['completed', 'error'].includes(task.status) || Boolean(state.pollError);
    if (progress === null) taskProgress.removeAttribute('value');
    else taskProgress.value = progress;
    taskFilename.textContent = task.status === 'completed' ? `Сохранено: ${task.filepath || task.filename || 'папка ~/Downloads/YouTube'}` : '';
    resumeTask.hidden = !state.pollError || ['completed', 'error'].includes(task.status);
    resumeTask.disabled = state.pollBusy;
    renderVideo();
    positionPanel();
  }

  function schedulePoll(delay = 1000) {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = window.setTimeout(pollTask, delay);
  }

  async function pollTask() {
    const task = state.activeTask;
    if (!task?.task_id || state.pollBusy || ['completed', 'error'].includes(task.status)) return;
    state.pollBusy = true;
    resumeTask.disabled = true;
    let nextDelay = null;
    try {
      const update = await api(`/api/tasks/${encodeURIComponent(task.task_id)}`);
      if (state.activeTask !== task) return;
      Object.assign(task, update);
      state.pollError = '';
      state.pollFailures = 0;
      if (!['completed', 'error'].includes(task.status)) nextDelay = 1000;
    } catch (error) {
      if (state.activeTask !== task) return;
      if (error.status === 404) {
        task.status = 'error';
        task.error = 'Сервер больше не знает эту задачу. Возможно, он был перезапущен; проверьте папку загрузок перед новой попыткой.';
        state.pollError = '';
      } else {
        state.pollFailures += 1;
        state.pollError = `Связь с сервером потеряна. Задача могла продолжить скачивание. ${error.message}`;
        if (state.pollFailures < 3) nextDelay = state.pollFailures * 2000;
      }
    } finally {
      state.pollBusy = false;
      renderTask();
      if (nextDelay !== null) schedulePoll(nextDelay);
    }
  }

  async function startDownload() {
    if (!state.video || busy() || !qualitySelect.value) return;
    const video = { ...state.video };
    const quality = qualitySelect.value;
    state.starting = true;
    state.pollError = '';
    state.activeTask = { status: 'preparing', progress: null, videoTitle: video.title, videoId: video.id };
    renderTask();
    try {
      const result = await api('/api/download', { method: 'POST', data: { url: video.url, quality } });
      if (!result.task_id) throw new Error('Сервер не вернул идентификатор задачи.');
      state.activeTask.task_id = String(result.task_id);
      state.pollFailures = 0;
      schedulePoll(250);
    } catch (error) {
      state.activeTask.status = 'error';
      state.activeTask.error = error.message;
    } finally {
      state.starting = false;
      renderTask();
    }
  }

  function pageVideo() {
    let id = parseVideoId(location.href);
    // A player-state fallback is limited to video routes. It cannot resurrect a
    // stale video button on the home page after an SPA navigation.
    const route = location.pathname === '/watch' || location.pathname.startsWith('/shorts/');
    if (!route) return null;
    const address = new URL(location.href);
    const playerFallbackAllowed = (location.pathname === '/watch' && !address.searchParams.has('v')) || location.pathname === '/shorts/';
    let playerData;
    try { playerData = document.getElementById('movie_player')?.getVideoData?.(); } catch { /* The player API is optional. */ }
    if (!id && playerFallbackAllowed && VIDEO_ID_PATTERN.test(playerData?.video_id || '')) id = playerData.video_id;
    if (!id) return null;
    let title = playerData?.video_id === id ? playerData.title : '';
    const activeShort = document.querySelector('ytd-reel-video-renderer[is-active]');
    if (!title && location.pathname.startsWith('/shorts/')) {
      if (!activeShort?.getAttribute('video-id') || activeShort.getAttribute('video-id') === id) {
        title = activeShort?.querySelector('#video-title, .ytShortsVideoTitleViewModelShortsVideoTitle, .title, h2')?.textContent?.trim();
      }
    } else if (!title) {
      const watchPage = document.querySelector('ytd-watch-flexy');
      if (!watchPage?.getAttribute('video-id') || watchPage.getAttribute('video-id') === id) {
        title = document.querySelector('ytd-watch-metadata h1, h1.ytd-watch-metadata, #title h1')?.textContent?.trim();
      }
    }
    const staleDocumentTitle = state.video && (state.video.id !== id || state.video.awaitingDocumentTitle)
      && state.video.pageDocumentTitle === document.title;
    if (!title && !staleDocumentTitle) title = document.title.replace(/\s*[-–]\s*YouTube\s*$/i, '').trim();
    return { id, url: canonicalVideoUrl(id), title: title || `YouTube_${id}`, pageDocumentTitle: document.title, awaitingDocumentTitle: Boolean(staleDocumentTitle && !title) };
  }

  function insertionTarget() {
    if (location.pathname.startsWith('/shorts/')) {
      return document.querySelector('ytd-reel-video-renderer[is-active] #actions')
        || document.querySelector('ytd-reel-video-renderer[is-active] .ytShortsVideoMetadataRendererHost');
    }
    return document.querySelector('ytd-watch-metadata #top-level-buttons-computed')
      || document.querySelector('#above-the-fold #top-level-buttons-computed')
      || document.querySelector('ytd-watch-metadata #actions')
      || document.querySelector('ytm-slim-video-action-bar-renderer');
  }

  function syncTheme() {
    const dark = document.documentElement.hasAttribute('dark') || document.documentElement.getAttribute('dark') === 'true'
      || document.body?.hasAttribute('dark');
    host.toggleAttribute('data-dark', dark);
  }

  function reconcilePage() {
    const video = pageVideo();
    if (!video) {
      if (state.video) {
        state.revision += 1;
        cancelPageRequests();
        clearCovers();
        state.video = null;
        state.formats = null;
        state.formatsLoading = false;
        state.formatsError = '';
      }
      closePanel(false);
      if (host.isConnected) host.remove();
      return;
    }
    if (state.video?.id !== video.id) {
      state.revision += 1;
      cancelPageRequests();
      clearCovers();
      state.video = video;
      state.formats = null;
      state.formatsLoading = false;
      state.formatsError = '';
      renderVideo();
      renderCovers();
      if (state.opened) {
        if (state.tab === 'cover') ensureCovers();
        else ensureFormats();
      }
    } else if (!state.formats && video.title !== state.video.title) {
      state.video.title = video.title;
      videoTitle.textContent = video.title;
    }
    state.video.pageDocumentTitle = video.pageDocumentTitle;
    state.video.awaitingDocumentTitle = video.awaitingDocumentTitle;
    const target = insertionTarget();
    if (target) {
      if (host.parentNode !== target) target.append(host);
      host.style.position = 'relative';
      host.style.right = '';
      host.style.bottom = '';
    } else {
      if (host.parentNode !== document.body) document.body.append(host);
      host.style.position = 'fixed';
      host.style.right = '16px';
      host.style.bottom = '20px';
    }
    syncTheme();
    positionPanel();
  }

  function positionPanel() {
    if (!state.opened || !host.isConnected) return;
    const anchor = button.getBoundingClientRect();
    const viewportHeight = window.innerHeight;
    const viewportWidth = window.innerWidth;
    const width = Math.min(380, viewportWidth - 24);
    const height = Math.min(panel.scrollHeight, viewportHeight - 24);
    const left = Math.max(12, Math.min(anchor.right - width, viewportWidth - width - 12));
    const below = anchor.bottom + 8;
    const top = below + height <= viewportHeight - 12 ? below : Math.max(12, anchor.top - height - 8);
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
  }

  function selectTab(tab, focus = false) {
    state.tab = tab;
    rememberValue('preferredTab', tab);
    const videoSelected = tab === 'video';
    videoTab.setAttribute('aria-selected', String(videoSelected));
    coverTab.setAttribute('aria-selected', String(!videoSelected));
    videoTab.tabIndex = videoSelected ? 0 : -1;
    coverTab.tabIndex = videoSelected ? -1 : 0;
    videoView.hidden = !videoSelected;
    coverView.hidden = videoSelected;
    if (focus) (videoSelected ? videoTab : coverTab).focus();
    if (state.opened) {
      if (videoSelected) ensureFormats();
      else ensureCovers();
    }
    positionPanel();
  }

  function openPanel() {
    if (!state.video) return;
    state.opened = true;
    panel.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    renderVideo();
    renderCovers();
    renderTask();
    selectTab(state.tab);
    positionPanel();
  }

  function closePanel(restoreFocus = true) {
    state.opened = false;
    panel.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    if (restoreFocus && host.isConnected) button.focus();
  }

  button.addEventListener('click', () => state.opened ? closePanel() : openPanel());
  closeButton.addEventListener('click', () => closePanel());
  videoTab.addEventListener('click', () => selectTab('video'));
  coverTab.addEventListener('click', () => selectTab('cover'));
  tabs.addEventListener('keydown', (event) => {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      selectTab(event.key === 'Home' ? 'video' : event.key === 'End' ? 'cover' : state.tab === 'video' ? 'cover' : 'video', true);
    }
  });
  startButton.addEventListener('click', startDownload);
  qualitySelect.addEventListener('change', () => rememberValue('preferredQuality', qualitySelect.value));
  refreshButton.addEventListener('click', () => ensureFormats(true));
  saveToken.addEventListener('click', () => {
    const newToken = tokenInput.value.trim();
    if (!newToken) {
      settings.open = true;
      return;
    }
    state.token = newToken;
    rememberValue('apiToken', state.token);
    tokenInput.value = '';
    tokenInput.placeholder = 'Токен сохранён. Введите новый для замены.';
    state.formatsGeneration += 1;
    cancelPageRequests(['metadata']);
    state.formats = null;
    state.formatsLoading = false;
    state.formatsError = '';
    if (state.token) {
      settings.open = false;
      ensureFormats(true);
      if (state.activeTask?.task_id && busy()) {
        state.pollFailures = 0;
        schedulePoll(250);
      }
    } else {
      settings.open = true;
      renderVideo();
    }
  });
  coverSelect.addEventListener('change', updateCoverPreview);
  coverButton.addEventListener('click', downloadCover);
  resumeTask.addEventListener('click', () => {
    state.pollFailures = 0;
    state.pollError = '';
    pollTask();
  });
  document.addEventListener('pointerdown', (event) => {
    if (state.opened && !event.composedPath().includes(host)) closePanel(false);
  });
  document.addEventListener('keydown', (event) => {
    if (state.opened && event.key === 'Escape') {
      event.preventDefault();
      closePanel();
    }
  });
  window.addEventListener('resize', positionPanel, { passive: true });
  window.addEventListener('scroll', positionPanel, { passive: true, capture: true });

  let reconcileTimer = null;
  function scheduleReconcile() {
    // At most one small DOM reconciliation per 180 ms. Changes inside the
    // shadow root do not reach this observer, preventing render feedback loops.
    if (reconcileTimer !== null) return;
    reconcileTimer = window.setTimeout(() => {
      reconcileTimer = null;
      reconcilePage();
    }, 180);
  }
  document.addEventListener('yt-navigate-finish', reconcilePage);
  document.addEventListener('yt-navigate-start', scheduleReconcile);
  window.addEventListener('popstate', reconcilePage);
  const domObserver = new MutationObserver(scheduleReconcile);
  domObserver.observe(document.body, { childList: true, subtree: true });
  const themeObserver = new MutationObserver(syncTheme);
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['dark'] });
  themeObserver.observe(document.body, { attributes: true, attributeFilter: ['dark'] });
  selectTab(state.tab);
  renderVideo();
  renderCovers();
  reconcilePage();
})();

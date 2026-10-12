'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  parseVideoId,
  canonicalVideoUrl,
  sanitizeFilename,
  validThumbnailDimensions,
  normalizeProgress,
  COVER_CANDIDATES,
} = require('../userscript/youtube-downloader.user.js');

const VIDEO_ID = 'BaW_jenozKc';

test('current watch and Shorts URLs identify the same video', () => {
  for (const url of [
    `https://www.youtube.com/watch?v=${VIDEO_ID}`,
    `https://youtube.com/watch?v=${VIDEO_ID}&feature=share`,
    `https://m.youtube.com/watch?v=${VIDEO_ID}&t=12`,
    `https://www.youtube.com/shorts/${VIDEO_ID}`,
    `https://www.youtube.com/shorts/${VIDEO_ID}/?feature=share`,
  ]) {
    assert.equal(parseVideoId(url), VIDEO_ID, url);
  }
});

test('invalid hosts, credentials, paths and video IDs are rejected', () => {
  for (const url of [
    `https://www.youtube.com.evil.example/watch?v=${VIDEO_ID}`,
    `https://evil.example/watch?v=${VIDEO_ID}`,
    `https://user:password@www.youtube.com/watch?v=${VIDEO_ID}`,
    `file://www.youtube.com/watch?v=${VIDEO_ID}`,
    `https://www.youtube.com/shorts/${VIDEO_ID}/extra`,
    `https://www.youtube.com/watch?v=${VIDEO_ID}&v=AAAAAAAAAAA`,
    `https://www.youtube.com/embed/${VIDEO_ID}`,
    'https://www.youtube.com/watch?v=invalid',
    'https://www.youtube.com/shorts/',
    'https://www.youtube.com/',
    'javascript:alert(1)',
    '',
  ]) {
    assert.equal(parseVideoId(url), null, url);
  }
});

test('canonical video URLs cannot contain arbitrary client input', () => {
  assert.equal(canonicalVideoUrl(VIDEO_ID), `https://www.youtube.com/watch?v=${VIDEO_ID}`);
  for (const id of ['', 'short', '../outside', `${VIDEO_ID}&url=evil`, null, undefined]) {
    assert.throws(() => canonicalVideoUrl(id));
  }
});

test('thumbnail filenames are safe on Windows and preserve readable titles', () => {
  const safe = sanitizeFilename('../bad\\name:\u0000\r\n*?"<>|... ');
  assert.ok(safe);
  assert.doesNotMatch(safe, /[<>:"/\\|?*\u0000-\u001f\u007f]/);
  assert.doesNotMatch(safe, /[. ]$/);
  for (const title of ['', '...', 'CON', 'NUL.jpg', 'COM1', 'LPT9.txt']) {
    const name = sanitizeFilename(title);
    assert.ok(name);
    assert.doesNotMatch(name, /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i);
  }
  assert.match(sanitizeFilename('Пример видео 😀'), /Пример видео/);
});

test('long Unicode thumbnail basenames fit a 255-byte filesystem limit', () => {
  for (const title of ['😀'.repeat(200), '示例视频'.repeat(100), 'Название'.repeat(100)]) {
    const safe = sanitizeFilename(title);
    const filename = `${safe}_thumbnail.jpg`;
    assert.ok(Buffer.byteLength(filename, 'utf8') <= 255);
    assert.equal(Buffer.from(safe, 'utf8').toString('utf8'), safe);
  }
});

test('maxres accepts actual higher resolutions and rejects YouTube placeholders', () => {
  const maxres = COVER_CANDIDATES.find((candidate) => candidate.key === 'maxresdefault');
  assert.ok(maxres);
  assert.equal(validThumbnailDimensions(maxres, 1280, 720), true);
  assert.equal(validThumbnailDimensions(maxres, 1920, 1080), true);
  for (const [width, height] of [[120, 90], [480, 360], [1280, 90], [0, 0], [NaN, 720], [Infinity, 720]]) {
    assert.equal(validThumbnailDimensions(maxres, width, height), false);
  }
});

test('standard official thumbnail URLs must return their advertised dimensions', () => {
  for (const candidate of COVER_CANDIDATES.filter((entry) => entry.key !== 'maxresdefault')) {
    assert.equal(validThumbnailDimensions(candidate, candidate.width, candidate.height), true);
    assert.equal(validThumbnailDimensions(candidate, 120, 90), false);
    assert.equal(validThumbnailDimensions(candidate, candidate.width, candidate.height - 1), false);
  }
});

test('progress handles unknown and nonfinite values without a misleading percentage', () => {
  for (const value of [null, undefined, '', '25', NaN, Infinity, -Infinity, {}]) {
    assert.equal(normalizeProgress(value), null);
  }
  assert.equal(normalizeProgress(-1), 0);
  assert.equal(normalizeProgress(25.5), 25.5);
  assert.equal(normalizeProgress(200), 100);
});

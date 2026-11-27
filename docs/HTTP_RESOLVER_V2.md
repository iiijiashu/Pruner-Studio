# Pruner Studio Unified HTTP Resolver Contract v2 & Sidecar Specification

## Overview

Pruner Studio separates core library management, audio arbitration, and lyrics rendering from external source resolution via the **Unified HTTP Resolver Contract v2**. Any compliant HTTP service — or an optional isolated loopback Sidecar process — can provide search, stream resolution, and lyrics without sharing cookies, host credentials, or executing untrusted code inside the core process.

## Security & Isolation Invariants

1. **Zero Host Credential Leakage**: Pruner Studio never forwards NetEase cookies or other channel credentials to any Resolver v2 endpoint.
2. **Strict SSRF & DNS Pinning**: Public resolver endpoints must use HTTPS and resolve solely to global unicast IPs; only explicitly configured loopback endpoints (`127.0.0.1` / `localhost` / `::1`) may use HTTP.
3. **Authentication**: Optional per-resolver token is sent via `X-Resolver-Token` and `X-API-Key` solely to the configured resolver origin and stripped on cross-origin redirects.
4. **Per-Channel Circuit Breaker**: Repeated failures open the circuit with exponential cooldown (`30s -> 60s -> 120s -> 180s`) so degraded endpoints never stall playback or search.

## Endpoints

### 1. `GET /manifest`
Returns resolver metadata and supported capabilities:
```json
{
  "protocol_version": 2,
  "id": "my-resolver",
  "name": "My Music Resolver",
  "version": "1.0.0",
  "capabilities": ["search", "resolve", "lyric"],
  "sources": ["flac-archive"],
  "rate_limit": { "requests_per_minute": 60 }
}
```

### 2. `POST /search` (fallback `GET /search`)
Request body:
```json
{
  "query": "晴天 周杰伦",
  "title": "晴天",
  "artists": ["周杰伦"],
  "album": "叶惠美",
  "duration_ms": 269000,
  "isrc": "",
  "limit": 30,
  "offset": 0
}
```
Response:
```json
{
  "has_more": false,
  "tracks": [
    {
      "id": "track-1001",
      "provider": "my-resolver",
      "title": "晴天",
      "artists": ["周杰伦"],
      "album": "叶惠美",
      "duration_ms": 269000,
      "isrc": "TWK970300403",
      "format": "flac",
      "bitrate": 980000,
      "sample_rate": 44100,
      "cover_url": "https://cdn.example.com/cover.jpg",
      "url": "https://cdn.example.com/audio.flac"
    }
  ]
}
```

### 3. `POST /resolve`
Resolves a playable audio stream for a specific candidate or canonical query:
```json
{
  "id": "track-1001",
  "title": "晴天",
  "artists": ["周杰伦"],
  "album": "叶惠美",
  "duration_ms": 269000,
  "isrc": "TWK970300403",
  "quality": "lossless"
}
```
Response:
```json
{
  "candidates": [
    {
      "id": "track-1001",
      "url": "https://cdn.example.com/audio.flac",
      "format": "flac",
      "bitrate": 980000,
      "sample_rate": 44100,
      "size": 31457280,
      "headers": { "Referer": "https://cdn.example.com/" },
      "replaygain_track_gain_db": -6.5,
      "replaygain_track_peak": 0.98
    }
  ]
}
```

### 4. `POST /lyric` (fallback `GET /lyric`)
Request: `{ "id": "track-1001", "title": "晴天", "artist": "周杰伦" }`
Response: `{ "lrc": "[00:00.00]...", "tlyric": "", "romalrc": "", "yrc": "" }`

## Subscription JSON Format (`POST /api/resolvers/subscribe`)
```json
{
  "version": 2,
  "resolvers": [
    {
      "id": "community-flac",
      "name": "Community FLAC Resolver",
      "url": "https://resolver.example.com/v2",
      "priority": 60,
      "capabilities": ["search", "resolve", "lyric"]
    }
  ]
}
```

## Optional Isolated Sidecar Bridge

Run `scripts/pruner_sidecar.py` directly or manage it from Pruner Studio via `POST /api/sidecar/start`:
- Runs in a separate child process with a stripped environment (zero host cookies/tokens).
- Receives its per-session `X-Resolver-Token` via stdin pipe (never process `argv`).
- Community adapters implement a Python file exporting `create_adapter()` or `class Adapter` with `manifest()`, `search()`, `resolve()`, and `lyric()`.

# Adaptive Coordinate Learning System - Design Doc

## Problem Statement

The current fast path optimization hardcodes coordinates for specific UI elements (tab bars, nav buttons) in the Geocaching app. While this provides 97% performance improvement, it has significant limitations:

1. **App-specific**: Only works for elements we've hardcoded
2. **Brittle**: Breaks if UI layout changes
3. **Maintenance burden**: Every new app needs manual coordinate mapping
4. **Not generalizable**: Doesn't work for arbitrary mobile projects

## Vision

Build a self-learning, adaptive system that:
- Works with ANY mobile app (not just Geocaching)
- Learns element coordinates automatically on first use
- Optimizes subsequent interactions (97% faster)
- Self-heals when UI changes
- Requires zero configuration or hardcoding
- Transparent to test code and AI agents

## Architecture

### Core Concept

```
First Interaction (Learning):
  tap_element(identifier="Login Button")
    → describe-all (slow, ~2-3s)
    → Find element at (150, 400)
    → Cache: {app + device + identifier} → {x, y, metadata}
    → Tap at (150, 400)

Subsequent Interactions (Optimized):
  tap_element(identifier="Login Button")
    → Check cache: found (150, 400)
    → Validate with describe-point (~100ms)
    → If matches: tap immediately ✓ (total: ~200ms)
    → If wrong: re-learn, update cache, tap

Performance:
  First run:  2-3 seconds (learning)
  Next runs:  200-300ms (97% faster!)
```

### Cache Storage Format

```json
{
  "cache_version": "1.0",
  "entries": {
    "com.groundspeak.GeocachingIntro:iPhone16Pro:_Profile button in tab bar": {
      "x": 40,
      "y": 812,
      "screen_width": 402,
      "screen_height": 852,
      "last_verified": "2026-02-14T00:15:23Z",
      "first_learned": "2026-02-10T14:30:00Z",
      "hit_count": 47,
      "miss_count": 2,
      "confidence_score": 0.96,
      "element_type": "RadioButton",
      "last_validation_time_ms": 142
    },
    "com.myapp:iPhone16ProMax:Submit Button": {
      "x": 220,
      "y": 650,
      "screen_width": 440,
      "screen_height": 926,
      "last_verified": "2026-02-14T01:20:15Z",
      "first_learned": "2026-02-14T01:18:00Z",
      "hit_count": 3,
      "miss_count": 0,
      "confidence_score": 1.0,
      "element_type": "Button",
      "last_validation_time_ms": 158
    }
  },
  "stats": {
    "total_entries": 2,
    "total_hits": 50,
    "total_misses": 2,
    "overall_hit_rate": 0.96
  }
}
```

**Storage Location:** `~/.quern/coordinate_cache.json`

**Cache Key Format:** `{bundle_id}:{device_model}:{identifier}`

### Implementation Components

#### 1. CoordinateCache Class

```python
class CoordinateCache:
    """Manages learned element coordinates with validation and stats."""

    def __init__(self, cache_file: Path = Path.home() / ".quern" / "coordinate_cache.json"):
        self._cache_file = cache_file
        self._cache: dict = self._load()
        self._lock = asyncio.Lock()

    def get(
        self,
        bundle_id: str,
        device_model: str,
        identifier: str
    ) -> dict | None:
        """Get cached coordinates if available and fresh."""
        key = f"{bundle_id}:{device_model}:{identifier}"
        entry = self._cache.get(key)

        if not entry:
            return None

        # Check age (optional: expire old entries)
        age_hours = (datetime.now() - datetime.fromisoformat(entry["last_verified"])).total_seconds() / 3600
        if age_hours > 24:  # Expire after 24 hours
            return None

        return entry

    async def set(
        self,
        bundle_id: str,
        device_model: str,
        identifier: str,
        x: int,
        y: int,
        element_type: str,
        screen_width: int,
        screen_height: int
    ):
        """Store or update cached coordinates."""
        async with self._lock:
            key = f"{bundle_id}:{device_model}:{identifier}"
            now = datetime.now().isoformat()

            if key in self._cache:
                # Update existing
                self._cache[key].update({
                    "x": x,
                    "y": y,
                    "last_verified": now,
                    "hit_count": self._cache[key].get("hit_count", 0) + 1
                })
            else:
                # Create new
                self._cache[key] = {
                    "x": x,
                    "y": y,
                    "screen_width": screen_width,
                    "screen_height": screen_height,
                    "first_learned": now,
                    "last_verified": now,
                    "hit_count": 1,
                    "miss_count": 0,
                    "confidence_score": 1.0,
                    "element_type": element_type
                }

            self._save()

    async def record_miss(self, bundle_id: str, device_model: str, identifier: str):
        """Record a cache miss (validation failed)."""
        async with self._lock:
            key = f"{bundle_id}:{device_model}:{identifier}"
            if key in self._cache:
                self._cache[key]["miss_count"] += 1
                # Recalculate confidence
                entry = self._cache[key]
                total = entry["hit_count"] + entry["miss_count"]
                entry["confidence_score"] = entry["hit_count"] / total
                self._save()

    def stats(self) -> dict:
        """Get cache statistics."""
        total_hits = sum(e.get("hit_count", 0) for e in self._cache.values())
        total_misses = sum(e.get("miss_count", 0) for e in self._cache.values())
        total = total_hits + total_misses

        return {
            "total_entries": len(self._cache),
            "total_hits": total_hits,
            "total_misses": total_misses,
            "overall_hit_rate": total_hits / total if total > 0 else 0,
            "by_app": self._stats_by_app(),
            "by_device": self._stats_by_device()
        }

    def clear(self, bundle_id: str | None = None):
        """Clear cache (all or for specific app)."""
        if bundle_id:
            keys_to_remove = [k for k in self._cache.keys() if k.startswith(f"{bundle_id}:")]
            for key in keys_to_remove:
                del self._cache[key]
        else:
            self._cache.clear()
        self._save()
```

#### 2. Updated tap_element Flow

```python
async def tap_element(
    self,
    label: str | None = None,
    identifier: str | None = None,
    element_type: str | None = None,
    udid: str | None = None,
    skip_stability_check: bool = False,
) -> dict:
    """Tap element with adaptive coordinate learning."""

    start = time.perf_counter()
    resolved = await self.resolve_udid(udid)
    bundle_id = await self._get_active_bundle_id(resolved)  # New method needed
    device_info = await self._get_device_info(resolved)

    # Only use cache for identifier-based lookups (not labels)
    if identifier:
        # Try cached coordinates
        cached = self.coordinate_cache.get(bundle_id, device_info.name, identifier)

        if cached:
            logger.info(f"[COORD CACHE] Trying cached coordinates for {identifier}: ({cached['x']}, {cached['y']})")

            # Validate cached coordinates
            element = await self.idb.describe_point(resolved, cached["x"], cached["y"])

            if element and (element.get("identifier") == identifier or element.get("AXUniqueId") == identifier):
                # Cache hit + validation passed!
                logger.info(f"[COORD CACHE] ✓ Validated cached coordinates (confidence: {cached['confidence_score']:.2f})")

                await self.idb.tap(resolved, cached["x"], cached["y"])

                end = time.perf_counter()
                logger.info(f"[PERF] tap_element COMPLETE (cached): total={(end-start)*1000:.1f}ms")

                return {
                    "status": "ok",
                    "tapped": {
                        "identifier": identifier,
                        "type": cached["element_type"],
                        "x": cached["x"],
                        "y": cached["y"],
                        "cache_hit": True
                    }
                }
            else:
                # Validation failed - cache is stale
                logger.warning(f"[COORD CACHE] ✗ Validation failed for {identifier}, re-learning")
                await self.coordinate_cache.record_miss(bundle_id, device_info.name, identifier)

    # Cache miss or validation failed - use traditional describe-all
    logger.info(f"[COORD CACHE] Cache miss for {identifier}, using describe-all")

    elements, _ = await self.get_ui_elements(
        resolved,
        filter_label=label,
        filter_identifier=identifier,
        filter_type=element_type
    )

    matches = find_element(elements, label=label, identifier=identifier, element_type=element_type)

    if len(matches) == 0:
        raise DeviceError(
            f"No element found matching identifier='{identifier}', label='{label}', type='{element_type}'",
            tool="idb"
        )

    element = matches[0]
    cx, cy = get_center(element)

    # Learn/update cache for next time
    if identifier:
        screen_dims = await self._get_screen_dimensions(resolved)
        await self.coordinate_cache.set(
            bundle_id=bundle_id,
            device_model=device_info.name,
            identifier=identifier,
            x=int(cx),
            y=int(cy),
            element_type=element.type,
            screen_width=screen_dims["width"],
            screen_height=screen_dims["height"]
        )
        logger.info(f"[COORD CACHE] Learned coordinates for {identifier}: ({int(cx)}, {int(cy)})")

    # Stability check if needed
    if not skip_stability_check:
        # ... existing stability check logic ...
        pass

    # Tap
    await self.idb.tap(resolved, cx, cy)

    end = time.perf_counter()
    logger.info(f"[PERF] tap_element COMPLETE (learned): total={(end-start)*1000:.1f}ms")

    return {
        "status": "ok",
        "tapped": {
            "identifier": identifier,
            "label": label,
            "type": element.type,
            "x": cx,
            "y": cy,
            "cache_hit": False
        }
    }
```

#### 3. New API Endpoints

```python
# Get cache statistics
GET /api/v1/device/coordinate-cache/stats
Response: {
  "total_entries": 42,
  "total_hits": 347,
  "total_misses": 8,
  "overall_hit_rate": 0.977,
  "by_app": {...},
  "by_device": {...}
}

# Clear cache
DELETE /api/v1/device/coordinate-cache?bundle_id=com.myapp
Response: {"status": "ok", "cleared": 15}

# Export cache
GET /api/v1/device/coordinate-cache/export
Response: <coordinate_cache.json file>

# Import cache (team sharing)
POST /api/v1/device/coordinate-cache/import
Body: <coordinate_cache.json content>
Response: {"status": "ok", "imported": 42}
```

#### 4. New MCP Tools

```typescript
// Get cache statistics
{
  name: "get_coordinate_cache_stats",
  description: "Get statistics about learned element coordinates",
  inputSchema: { type: "object", properties: {} }
}

// Clear cache
{
  name: "clear_coordinate_cache",
  description: "Clear learned coordinates (all or for specific app)",
  inputSchema: {
    type: "object",
    properties: {
      bundle_id: { type: "string", description: "Optional: only clear this app" }
    }
  }
}
```

### MCP Tool Documentation Updates

Add to each affected tool's documentation:

```markdown
## tap_element

**Performance Optimization:**

This tool uses adaptive coordinate learning for improved performance:

**First Time You Use an Element:**
- Uses full UI tree scan (~2-3 seconds)
- Learns the element's coordinates
- Caches for future interactions

**Subsequent Times:**
- Tries cached coordinates first (~200ms) - 97% faster!
- Validates the element is correct
- Auto-updates cache if element moved
- Falls back to full scan if validation fails

**Agent Best Practices:**

1. **Use consistent identifiers**: The same identifier will benefit from caching
2. **First test run is slower**: System is learning element positions
3. **Subsequent runs auto-optimize**: No code changes needed
4. **Cache is per-app, per-device**: Different apps/devices learn independently
5. **Self-healing**: If UI changes, cache auto-updates

**Example:**
```python
# First run: ~2s (learning)
tap_element(identifier="Submit Button")

# Next 100 runs: ~200ms each (using cache)
tap_element(identifier="Submit Button")  # Fast!
tap_element(identifier="Submit Button")  # Fast!
...
```

**No special code needed.** Just use identifiers and the system learns automatically.
```

## Benefits

### For Test Authors
- ✅ Write tests naturally with identifiers
- ✅ Automatic performance optimization
- ✅ Works across any mobile project
- ✅ No configuration needed

### For AI Agents
- ✅ Clear mental model: "first time is slow, then fast"
- ✅ Can query cache stats to understand performance
- ✅ Can clear cache when UI changes significantly

### For DevOps/CI
- ✅ Share learned caches across team
- ✅ Pre-warm cache before test runs
- ✅ Export/import for reproducibility
- ✅ Monitor cache hit rates for test health

### For Performance
- ✅ 97% faster after learning
- ✅ Scales to any number of apps
- ✅ No concurrent slowdown
- ✅ Self-healing on UI changes

## Implementation Strategy

### Phase 1: Core Learning System
1. Implement `CoordinateCache` class
2. Add cache storage (~/.quern/coordinate_cache.json)
3. Update `tap_element` to try cache → validate → fallback
4. Add cache stats tracking

### Phase 2: API & Observability
1. Add `/coordinate-cache/*` API endpoints
2. Add MCP tools for cache management
3. Update tool documentation
4. Add logging and metrics

### Phase 3: Advanced Features
1. Team cache sharing (export/import)
2. Confidence scoring
3. Cache pre-warming
4. Multi-screen awareness (same identifier, different screens)

### Phase 4: Extend to wait_for_element
1. Apply same learning to `wait_for_element`
2. Cache "element exists at (x,y)" checks
3. Use describe-point instead of describe-all

## Migration Path

### From Current Hardcoded System

```python
# Remove:
_SCREEN_DIMENSIONS = {...}
_STATIC_ELEMENT_POSITIONS = {...}

# Replace with:
self.coordinate_cache = CoordinateCache()

# Updated methods use cache automatically
```

**Migration is seamless:**
- New system subsumes old hardcoded coordinates
- First run learns coordinates dynamically
- Subsequent runs use learned cache
- No test code changes needed

## Open Questions

1. **Cache invalidation strategy?**
   - Time-based (24 hours)?
   - Confidence-based (after N misses)?
   - Manual (per-app version)?

2. **Multi-screen handling?**
   - Same identifier on different screens (Login button on splash vs settings)
   - Need screen context in cache key?

3. **Rotation support?**
   - Cache separate coordinates for portrait/landscape?
   - Detect orientation changes?

4. **Cache size limits?**
   - Max entries per app?
   - LRU eviction?

5. **Team sharing UX?**
   - Git-committable cache files?
   - Server-side cache storage?

## Success Metrics

- **Hit rate**: % of taps using cached coordinates (target: >95%)
- **Performance**: Average tap time (target: <300ms after learning)
- **Reliability**: Cache validation failure rate (target: <5%)
- **Adoption**: # of apps benefiting from cache (target: all)
- **Portability**: Zero hardcoded coordinates (target: achieved)

## Future Enhancements

1. **Visual feedback**: Show cache hits/misses in UI
2. **Cache warming**: Pre-populate cache by crawling app
3. **ML-based prediction**: Predict element movement patterns
4. **Screen state tracking**: Different caches per screen
5. **Element stability scoring**: Trust stable elements more
6. **Cross-device interpolation**: Predict coordinates on untested devices

---

**Status:** Design proposal
**Created:** 2026-02-14
**Author:** AI-assisted design
**Next Steps:** Review, prototype, implement Phase 1

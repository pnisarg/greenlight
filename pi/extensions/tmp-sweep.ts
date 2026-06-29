/**
 * Best-effort reclamation of greenlight's events temp dirs.
 *
 * The extension writes each run's event stream to a mkdtemp'd
 * `greenlight-events-*` dir and removes it when the tool call finishes. But the
 * gate is now spawned detached so it survives the pi window closing — on that
 * path the tool's own cleanup never runs and the dir leaks. There is no daemon,
 * so (like the Python worktree sweep) each new run reclaims stale dirs left by
 * hard-killed runs. Kept free of pi/TUI imports so it can be unit-tested.
 */

import { readdirSync, rmSync, statSync } from "node:fs";
import { join } from "node:path";

export const EVENTS_PREFIX = "greenlight-events-";

// A run never lasts hours; a dir older than this was orphaned by a pi that died
// before its rmSync ran. Wide margin so we never race a concurrent live run
// sharing the same temp root.
export const EVENTS_STALE_MS = 6 * 60 * 60 * 1000;

/**
 * Remove `greenlight-events-*` dirs under `root` whose mtime is older than
 * `staleMs`. Returns the count removed. Never throws: a missing root, a racing
 * sweep, or a permission error is swallowed so telemetry cleanup can't break a
 * run.
 */
export function sweepStaleEventDirs(
	root: string,
	staleMs: number = EVENTS_STALE_MS,
	now: number = Date.now(),
): number {
	let entries: string[];
	try {
		entries = readdirSync(root);
	} catch {
		return 0;
	}
	const cutoff = now - staleMs;
	let removed = 0;
	for (const name of entries) {
		if (!name.startsWith(EVENTS_PREFIX)) continue;
		const dir = join(root, name);
		try {
			if (statSync(dir).mtimeMs < cutoff) {
				rmSync(dir, { recursive: true, force: true });
				removed += 1;
			}
		} catch {
			// gone already / racing another sweep — ignore.
		}
	}
	return removed;
}

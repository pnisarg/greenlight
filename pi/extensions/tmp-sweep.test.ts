import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { EVENTS_PREFIX, sweepStaleEventDirs } from "./tmp-sweep.ts";

/** A scratch temp root so the sweep never touches the real /tmp during tests. */
function scratchRoot(): string {
	return mkdtempSync(join(tmpdir(), "gl-sweep-test-"));
}

function makeEventDir(root: string, suffix: string, ageMs: number): string {
	const dir = join(root, `${EVENTS_PREFIX}${suffix}`);
	mkdirSync(dir);
	writeFileSync(join(dir, "events.jsonl"), "{}\n");
	const when = new Date(Date.now() - ageMs);
	utimesSync(dir, when, when);
	return dir;
}

test("removes events dirs older than the stale window", () => {
	const root = scratchRoot();
	try {
		const old = makeEventDir(root, "old", 7 * 60 * 60 * 1000); // 7h
		const removed = sweepStaleEventDirs(root, 6 * 60 * 60 * 1000);
		assert.equal(removed, 1);
		assert.equal(existsSync(old), false);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("keeps fresh dirs (a concurrent live run is safe)", () => {
	const root = scratchRoot();
	try {
		const fresh = makeEventDir(root, "fresh", 60 * 1000); // 1 min
		const removed = sweepStaleEventDirs(root, 6 * 60 * 60 * 1000);
		assert.equal(removed, 0);
		assert.equal(existsSync(fresh), true);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("ignores unrelated temp dirs", () => {
	const root = scratchRoot();
	try {
		const other = join(root, "some-other-tool-xyz");
		mkdirSync(other);
		utimesSync(other, new Date(0), new Date(0)); // ancient
		const removed = sweepStaleEventDirs(root, 6 * 60 * 60 * 1000);
		assert.equal(removed, 0);
		assert.equal(existsSync(other), true);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("missing root is a no-op, not a throw", () => {
	const removed = sweepStaleEventDirs(join(tmpdir(), "gl-does-not-exist-zzz"));
	assert.equal(removed, 0);
});

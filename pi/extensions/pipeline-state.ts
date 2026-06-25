/**
 * Pure reducer + renderer for the greenlight pipeline event stream.
 *
 * greenlight (the Python gate) emits one JSON object per line to the file named
 * by GREENLIGHT_EVENTS. This module turns that stream into a snapshot of the
 * pipeline and renders it as the live tool-call card. It is deliberately free of
 * any pi/TUI imports so it can be unit-tested with plain node and reused for any
 * surface (card, widget, web).
 *
 * The handoff this visualizes: the coding agent authors the intent and hands off
 * to greenlight; the autonomous pipeline (lint -> review loop -> verify -> PR)
 * runs in a throwaway worktree; the result is handed back. The card's first row
 * is "intent (from agent)" and the last is the returned verdict, so the boundary
 * between agent and gate stays legible.
 */

export type StageStatus = "pending" | "running" | "done" | "fail" | "skip";

export type StageName = "intent" | "lint" | "review" | "verify" | "pr";

export const STAGE_ORDER: StageName[] = ["intent", "lint", "review", "verify", "pr"];

export interface Finding {
	severity: string;
	file: string;
	line: number | null;
	description: string;
	blocks: boolean;
}

export interface ReviewerState {
	name: string;
	status: StageStatus;
	findings: number | null;
	blocking: number | null;
	items: Finding[];
}

export interface PipelineState {
	branch: string;
	classification: string;
	fileCount: number;
	intentSource: "supplied" | "reconstructed" | null;
	intentText: string;
	stages: Record<StageName, StageStatus>;
	review: {
		round: number;
		maxRounds: number;
		fixes: number;
		reviewers: ReviewerState[];
	};
	verify: { target: string; status: StageStatus }[];
	pr: { status: string; url: string };
	passed: boolean | null;
	/** Epoch seconds of the first event seen for each stage (for elapsed time). */
	stageStart: Partial<Record<StageName, number>>;
	/** Epoch seconds of the last event seen overall (run "now" for elapsed). */
	lastTs: number;
	/** Which gate blocked, when the run failed. Null while running or on pass. */
	failedStage: StageName | null;
	/** Any line that failed to parse, kept for debugging; not rendered. */
	parseErrors: number;
}

export interface GreenlightEvent {
	type: string;
	[key: string]: unknown;
}

export function initialState(): PipelineState {
	return {
		branch: "",
		classification: "",
		fileCount: 0,
		intentSource: null,
		intentText: "",
		stages: { intent: "pending", lint: "pending", review: "pending", verify: "pending", pr: "pending" },
		review: { round: 0, maxRounds: 0, fixes: 0, reviewers: [] },
		verify: [],
		pr: { status: "", url: "" },
		passed: null,
		stageStart: {},
		lastTs: 0,
		failedStage: null,
		parseErrors: 0,
	};
}

const STAGE_FOR_EVENT: Record<string, StageName> = {
	run_start: "intent",
	intent: "intent",
	lint: "lint",
	review_round: "review",
	reviewer: "review",
	fix: "review",
	verify: "verify",
	pr: "pr",
};

function statusFor(s: string): StageStatus {
	if (s === "pass") return "done";
	if (s === "fail") return "fail";
	if (s === "skip") return "skip";
	return "pending";
}

/** Apply one event to the state, mutating and returning it. Order-tolerant. */
export function reduce(state: PipelineState, ev: GreenlightEvent): PipelineState {
	const ts = typeof ev.ts === "number" ? ev.ts : 0;
	if (ts) {
		state.lastTs = Math.max(state.lastTs, ts);
		const stage = STAGE_FOR_EVENT[ev.type];
		if (stage && state.stageStart[stage] === undefined) state.stageStart[stage] = ts;
	}
	switch (ev.type) {
		case "run_start": {
			state.branch = String(ev.branch ?? "");
			state.classification = String(ev.classification ?? "");
			state.fileCount = Array.isArray(ev.files) ? ev.files.length : 0;
			state.stages.intent = "running";
			break;
		}
		case "intent": {
			state.intentSource = ev.source === "supplied" ? "supplied" : "reconstructed";
			state.intentText = String(ev.text ?? "");
			state.stages.intent = "done";
			break;
		}
		case "lint": {
			state.stages.lint = statusFor(String(ev.status));
			break;
		}
		case "review_round": {
			state.stages.review = "running";
			state.review.round = Number(ev.round ?? 0);
			state.review.maxRounds = Number(ev.max_rounds ?? 0);
			break;
		}
		case "reviewer": {
			const name = String(ev.name ?? "");
			const findings = ev.findings == null ? null : Number(ev.findings);
			const blocking = ev.blocking == null ? null : Number(ev.blocking);
			let r = state.review.reviewers.find((x) => x.name === name);
			if (!r) {
				r = { name, status: "running", findings: null, blocking: null, items: [] };
				state.review.reviewers.push(r);
			}
			// findings == null marks "started"; a later event with counts marks "done".
			if (findings == null) {
				r.status = "running";
			} else {
				r.status = "done";
				r.findings = findings;
				r.blocking = blocking;
				r.items = parseItems(ev.items);
			}
			break;
		}
		case "fix": {
			state.review.fixes += 1;
			// A new fix round is coming; the reviewers will re-run.
			for (const r of state.review.reviewers) r.status = "running";
			break;
		}
		case "verify": {
			state.stages.review = settleReview(state);
			const target = String(ev.target ?? "");
			const status = statusFor(String(ev.status));
			const existing = state.verify.find((v) => v.target === target);
			if (existing) existing.status = status;
			else state.verify.push({ target, status });
			state.stages.verify = aggregateVerify(state);
			break;
		}
		case "pr": {
			state.stages.review = settleReview(state);
			state.pr.status = String(ev.status ?? "");
			state.pr.url = String(ev.url ?? "");
			state.stages.pr =
				state.pr.status === "open" || state.pr.status === "exists"
					? "done"
					: state.pr.status === "skip"
						? "skip"
						: state.pr.status === "fail"
							? "fail"
							: "pending";
			break;
		}
		case "run_end": {
			state.passed = Boolean(ev.passed);
			finalize(state);
			break;
		}
		default:
			break;
	}
	return state;
}

/** Once we leave the review loop cleanly, a still-running review stage is done. */
function settleReview(state: PipelineState): StageStatus {
	return state.stages.review === "running" ? "done" : state.stages.review;
}

function aggregateVerify(state: PipelineState): StageStatus {
	if (state.verify.some((v) => v.status === "fail")) return "fail";
	if (state.verify.length > 0 && state.verify.every((v) => v.status === "skip")) return "skip";
	if (state.verify.length > 0) return "done";
	return "running";
}

function finalize(state: PipelineState): void {
	if (state.passed) {
		// Everything up to PR cleared; promote any lingering running/pending
		// gates (PR keeps its explicit status — a failed PR doesn't fail the gate).
		for (const s of ["intent", "lint", "review", "verify"] as StageName[]) {
			if (state.stages[s] === "running" || state.stages[s] === "pending") state.stages[s] = "done";
		}
		return;
	}
	// Failed: the first gate still "running" is the one that blocked. Leave the
	// stages after it as pending so the card shows where the pipeline stopped.
	for (const s of ["intent", "lint", "review", "verify", "pr"] as StageName[]) {
		if (state.stages[s] === "running") {
			state.stages[s] = "fail";
			state.failedStage = s;
			break;
		}
	}
}

function parseItems(raw: unknown): Finding[] {
	if (!Array.isArray(raw)) return [];
	const out: Finding[] = [];
	for (const it of raw) {
		if (!it || typeof it !== "object") continue;
		const f = it as Record<string, unknown>;
		out.push({
			severity: String(f.severity ?? "warning"),
			file: String(f.file ?? ""),
			line: typeof f.line === "number" ? f.line : null,
			description: String(f.description ?? ""),
			blocks: Boolean(f.blocks),
		});
	}
	return out;
}

/** All blocking findings across reviewers (for the failure card). */
export function blockingFindings(state: PipelineState): Finding[] {
	return state.review.reviewers.flatMap((r) => r.items.filter((f) => f.blocks));
}

/** Elapsed seconds for a stage given a "now" (epoch seconds). 0 if unknown. */
export function stageElapsed(state: PipelineState, stage: StageName, now: number): number {
	const start = state.stageStart[stage];
	if (start === undefined) return 0;
	// A later stage's start bounds this stage's end; else use run "now".
	const order = STAGE_ORDER.indexOf(stage);
	let end = now;
	for (let i = order + 1; i < STAGE_ORDER.length; i++) {
		const s = state.stageStart[STAGE_ORDER[i]];
		if (s !== undefined) {
			end = s;
			break;
		}
	}
	return Math.max(0, end - start);
}

/** Feed a chunk of JSONL, returning the number of events applied. */
export function applyLines(state: PipelineState, text: string): number {
	let applied = 0;
	for (const line of text.split("\n")) {
		const trimmed = line.trim();
		if (!trimmed) continue;
		try {
			reduce(state, JSON.parse(trimmed) as GreenlightEvent);
			applied += 1;
		} catch {
			state.parseErrors += 1;
		}
	}
	return applied;
}

const GLYPH: Record<StageStatus, string> = {
	pending: "·",
	running: "⟳",
	done: "✓",
	fail: "✗",
	skip: "⊘",
};

/** Color category per status, mapped to theme colors by the caller. */
export type ColorName = "ok" | "fail" | "running" | "dim" | "muted" | "accent";

const COLOR: Record<StageStatus, ColorName> = {
	pending: "dim",
	running: "running",
	done: "ok",
	fail: "fail",
	skip: "dim",
};

export interface Painter {
	ok: (s: string) => string;
	fail: (s: string) => string;
	running: (s: string) => string;
	dim: (s: string) => string;
	muted: (s: string) => string;
	accent: (s: string) => string;
	bold: (s: string) => string;
}

/** Identity painter — useful for tests and non-TUI output. */
export const plain: Painter = {
	ok: (s) => s,
	fail: (s) => s,
	running: (s) => s,
	dim: (s) => s,
	muted: (s) => s,
	accent: (s) => s,
	bold: (s) => s,
};

function paint(p: Painter, name: ColorName, s: string): string {
	return p[name](s);
}

export interface RenderOptions {
	/** "now" in epoch seconds, for elapsed-time annotations. 0 disables them. */
	now?: number;
	/** Glyph to use for the single running stage (animated spinner frame). */
	spinner?: string;
	/** Expand blocking findings inline under their reviewer. */
	expanded?: boolean;
}

function fmtElapsed(seconds: number): string {
	const s = Math.round(seconds);
	if (s < 60) return `${s}s`;
	const m = Math.floor(s / 60);
	return `${m}m${String(s % 60).padStart(2, "0")}s`;
}

const STAGE_LABEL: Record<StageName, string> = {
	intent: "intent",
	lint: "lint",
	review: "review",
	verify: "verify",
	pr: "PR",
};

function stageDetail(state: PipelineState, stage: StageName): string {
	switch (stage) {
		case "intent":
			return state.intentSource ? `${state.intentSource} by agent` : "";
		case "review": {
			if (state.review.maxRounds) {
				const fixes = state.review.fixes ? `, ${state.review.fixes} fix${state.review.fixes > 1 ? "es" : ""}` : "";
				return `round ${state.review.round}/${state.review.maxRounds}${fixes}`;
			}
			return "";
		}
		case "verify":
			return state.verify.map((v) => `${v.target} ${v.status}`).join(", ");
		case "pr":
			return state.pr.url && state.pr.status !== "fail" ? state.pr.url : state.pr.status;
		default:
			return "";
	}
}

/**
 * Render the pipeline as boxed card lines. `p` maps semantic colors to the
 * active theme (or identity for plain text). Returns an array of lines.
 */
export function renderCard(state: PipelineState, p: Painter = plain, opts: RenderOptions = {}): string[] {
	const rows: string[] = [];
	const now = opts.now ?? 0;
	const header =
		state.branch
			? `${state.branch}  ·  ${state.classification || "?"}  ·  ${state.fileCount} file${state.fileCount === 1 ? "" : "s"}`
			: "starting…";
	rows.push(p.bold("greenlight ") + p.muted(header));

	for (const stage of STAGE_ORDER) {
		const st = state.stages[stage];
		const glyphChar = st === "running" && opts.spinner ? opts.spinner : GLYPH[st];
		const glyph = paint(p, COLOR[st], glyphChar);
		const label = STAGE_LABEL[stage].padEnd(7);
		let line = `  ${glyph} ${label}`;
		const detail = stageDetail(state, stage);
		if (detail) line += " " + p.dim(detail);
		// Elapsed time on the active (or finished) stage.
		if (now && st !== "pending") {
			const secs = stageElapsed(state, stage, now);
			if (secs >= 1) line += " " + p.muted(`(${fmtElapsed(secs)})`);
		}
		rows.push(line);
		// Nested reviewer lines under the review stage.
		if (stage === "review" && state.review.reviewers.length) {
			for (const r of state.review.reviewers) {
				const rGlyph = r.status === "running" && opts.spinner ? opts.spinner : GLYPH[r.status];
				const g = paint(p, COLOR[r.status], rGlyph);
				let sub = `      ${g} ${r.name.padEnd(10)}`;
				if (r.status === "done" && r.findings != null) {
					sub += " " + p.dim(`${r.findings} finding${r.findings === 1 ? "" : "s"}, ${r.blocking} blocking`);
				} else if (r.status === "running") {
					sub += " " + p.dim("running…");
				}
				rows.push(sub);
				// Expand-on-demand: blocking findings under the reviewer.
				if (opts.expanded) {
					for (const f of r.items.filter((x) => x.blocks)) {
						const loc = f.file + (f.line != null ? `:${f.line}` : "");
						rows.push("        " + p.fail("• ") + p.dim(`${loc} — ${f.description}`));
					}
				}
			}
		}
	}

	if (state.passed === false) {
		rows.push(p.fail("● FAILED — nothing forwarded"));
		const where = state.failedStage ?? failingStage(state);
		if (where) rows.push("  " + p.fail(`blocked at ${STAGE_LABEL[where]}`));
		// Always surface blocking findings on failure, even when collapsed.
		if (!opts.expanded) {
			for (const f of blockingFindings(state)) {
				const loc = f.file + (f.line != null ? `:${f.line}` : "");
				rows.push("  " + p.fail("• ") + p.dim(`[${f.severity}] ${loc} — ${f.description}`));
			}
		}
	} else if (state.passed === true) {
		rows.push(p.ok("● PASSED — handed back to agent"));
	}
	return rows;
}

/** Best-effort: the stage that blocked, when failedStage wasn't recorded. */
function failingStage(state: PipelineState): StageName | null {
	for (const s of STAGE_ORDER) {
		if (state.stages[s] === "fail") return s;
	}
	return null;
}

/** Compact one-line status for ctx.ui.setStatus (footer). */
export function statusLine(state: PipelineState, p: Painter = plain): string {
	if (state.passed === true) return p.ok("greenlight ✓ passed");
	if (state.passed === false) {
		const where = state.failedStage ?? failingStage(state);
		return p.fail(`greenlight ✗ failed${where ? ` at ${STAGE_LABEL[where]}` : ""}`);
	}
	// Running: name the active stage.
	let active: StageName | null = null;
	for (const s of STAGE_ORDER) {
		if (state.stages[s] === "running") active = s;
	}
	if (!active) return p.muted("greenlight …");
	let detail = STAGE_LABEL[active];
	if (active === "review" && state.review.maxRounds) {
		detail += ` ${state.review.round}/${state.review.maxRounds}`;
	}
	return p.accent(`greenlight ⟳ ${detail}`);
}

/** One-line plain-text verdict + summary for the LLM (the tool's text content). */
export function summarize(state: PipelineState): string {
	const lines: string[] = [];
	const head =
		state.passed == null
			? "greenlight: (incomplete)"
			: state.passed
				? "greenlight: PASSED"
				: "greenlight: FAILED";
	const ctx = state.branch ? ` — branch ${state.branch}, ${state.classification}, ${state.fileCount} files` : "";
	lines.push(head + ctx);

	if (state.intentSource) lines.push(`intent: ${state.intentSource}`);
	lines.push(`lint: ${state.stages.lint}`);

	if (state.review.maxRounds) {
		const rv = state.review.reviewers
			.map((r) => `${r.name} ${r.findings ?? "?"}/${r.blocking ?? "?"} blk`)
			.join(", ");
		const fixes = state.review.fixes ? `, ${state.review.fixes} fix round(s)` : "";
		lines.push(`review: ${state.stages.review} after round ${state.review.round}/${state.review.maxRounds}${fixes}${rv ? ` (${rv})` : ""}`);
	} else {
		lines.push(`review: ${state.stages.review}`);
	}

	if (state.verify.length) {
		lines.push(`verify: ${state.verify.map((v) => `${v.target} ${v.status}`).join(", ")}`);
	} else {
		lines.push(`verify: ${state.stages.verify}`);
	}

	if (state.pr.status) {
		lines.push(`pr: ${state.pr.status}${state.pr.url && state.pr.status !== "fail" ? ` ${state.pr.url}` : ""}`);
	}
	if (state.intentSource === "reconstructed") {
		lines.push("note: intent was reconstructed from the diff (none supplied) — review ran on a weaker signal.");
	}
	return lines.join("\n");
}

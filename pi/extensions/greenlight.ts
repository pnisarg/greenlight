/**
 * greenlight pi extension — live pipeline card.
 *
 * Registers a `greenlight_run` tool. When the coding agent invokes it (handing
 * off the intent it authored), the extension spawns `greenlight run`, points the
 * gate at a temp JSONL event file via GREENLIGHT_EVENTS, and tails that file to
 * drive a live tool-call card: intent → lint → review loop → verify → PR. The
 * card streams via onUpdate; the final text returned to the LLM is a compact
 * verdict. greenlight itself stays a deterministic Python orchestrator — this
 * only observes its event stream and renders it.
 */

import { spawn } from "node:child_process";
import { closeSync, mkdtempSync, openSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { keyHint, type ExtensionAPI, type Theme } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import { Type } from "typebox";

import {
	applyLines,
	initialState,
	type Painter,
	type PipelineState,
	renderCard,
	statusLine,
	summarize,
} from "./pipeline-state";

// Braille spinner; frame chosen from wall-clock so it animates across renders.
const SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
function spinnerFrame(): string {
	return SPINNER[Math.floor(Date.now() / 80) % SPINNER.length];
}

const PARAMS = Type.Object({
	intent: Type.String({
		description:
			"The intent you (the agent that made the change) authored: the goal in the user's terms, key decisions/tradeoffs, what you deliberately ruled in or out, and constraints. This is ground truth for the review loop. Be complete, not terse.",
	}),
	cwd: Type.Optional(
		Type.String({ description: "Repo working dir; defaults to the session cwd." }),
	),
});

export type GreenlightRunInput = {
	intent: string;
	cwd?: string;
};

/** Build a Painter from a pi theme. Kept here so pipeline-state stays TUI-free. */
function painterFor(theme: Theme): Painter {
	return {
		ok: (s) => theme.fg("success", s),
		fail: (s) => theme.fg("error", s),
		running: (s) => theme.fg("accent", s),
		dim: (s) => theme.fg("dim", s),
		muted: (s) => theme.fg("muted", s),
		accent: (s) => theme.fg("accent", s),
		bold: (s) => theme.bold(s),
	};
}

export default function greenlightExtension(pi: ExtensionAPI) {
	// Last pipeline state per tool call, so renderResult can keep showing the
	// card even when execute() throws on a gate failure (a thrown error result
	// carries no details.state). `done` marks the run finished so renderResult
	// stops animating the spinner / annotating live elapsed.
	const finalStates = new Map<string, { state: PipelineState; done: boolean }>();

	pi.registerTool({
		name: "greenlight_run",
		label: "greenlight",
		description:
			"Validate the current feature branch through the greenlight gate (intent → lint → review loop → verify → PR) and show a live pipeline card. Pass the intent you authored. The work must already be committed on a feature branch. Blocks until the pipeline finishes (can take minutes); the result text reports the verdict and per-stage outcomes.",
		promptSnippet: "Run the greenlight validation gate on the current branch with author-supplied intent",
		promptGuidelines: [
			"Use greenlight_run to validate/ship a committed feature branch; author a complete intent (goal, decisions, what you ruled in/out) since it is ground truth for the review loop.",
		],
		parameters: PARAMS,
		async execute(toolCallId, params, signal, onUpdate, ctx) {
			const cwd = params.cwd ?? ctx.cwd;
			const eventsDir = mkdtempSync(join(tmpdir(), "greenlight-events-"));
			const eventsFile = join(eventsDir, "events.jsonl");
			const stderrFile = join(eventsDir, "stderr.log");
			const state = initialState();
			finalStates.set(toolCallId, { state, done: false });
			const statusKey = `greenlight:${toolCallId}`;

			let lastSize = 0;
			const push = () => {
				onUpdate?.({
					content: [{ type: "text", text: summarize(state) }],
					details: { state },
				});
				if (ctx.hasUI) ctx.ui.setStatus(statusKey, statusLine(state, painterFor(ctx.ui.theme)));
			};
			const drain = () => {
				let size = 0;
				try {
					size = statSync(eventsFile).size;
				} catch {
					return;
				}
				if (size <= lastSize) return;
				let text = "";
				try {
					text = readFileSync(eventsFile, "utf8");
				} catch {
					return;
				}
				// Re-read from scratch and reset state each poll: events are cheap,
				// the reducer is order-tolerant, and this avoids partial-line splits.
				const fresh = initialState();
				applyLines(fresh, text);
				Object.assign(state, fresh);
				lastSize = size;
				push();
			};

			// Survive window close: spawn the gate detached (its own process group,
			// so a SIGHUP to pi doesn't cascade and kill the run mid-review) AND
			// redirect its stdout/stderr to a file rather than pipes back to pi.
			// Piping to a dying parent is the subtle trap: once pi exits, the gate's
			// next stderr write (every step/info line) would hit a broken pipe and
			// crash it. A file fd has no such coupling, so the run truly outlives the
			// window. We read progress from the events file and the failure tail from
			// stderrFile. The gate also writes events to the per-repo mirror, so
			// `greenlight watch` can re-attach to a run whose window has closed.
			const errFd = openSync(stderrFile, "a");
			const child = spawn("greenlight", ["run", "--intent-file", "-"], {
				cwd,
				env: { ...process.env, GREENLIGHT_EVENTS: eventsFile, GREENLIGHT_FORCE_COLOR: "0" },
				stdio: ["pipe", errFd, errFd],
				detached: true,
			});
			closeSync(errFd); // the child holds its own dup; we read the file by path
			child.unref();
			child.stdin?.write(params.intent);
			child.stdin?.end();

			const readStderrTail = (n: number): string => {
				try {
					return readFileSync(stderrFile, "utf8").trim().split("\n").slice(-n).join("\n");
				} catch {
					return "";
				}
			};

			const poll = setInterval(drain, 250);
			// Tick the spinner even when no new events arrive, so a long-running
			// stage visibly animates instead of looking hung.
			const tick = setInterval(() => {
				if (state.passed == null) push();
			}, 120);
			// Explicit cancel (Esc / tool abort) should stop the whole run. Signal
			// the process *group* (negative pid) since we spawned detached; fall back
			// to the bare child if the group send fails. The gate's SIGTERM handler
			// then unwinds its worktree cleanup.
			const onAbort = () => {
				try {
					if (child.pid) process.kill(-child.pid, "SIGTERM");
					else child.kill("SIGTERM");
				} catch {
					child.kill("SIGTERM");
				}
			};
			signal?.addEventListener("abort", onAbort, { once: true });

			const code: number = await new Promise((resolve) => {
				child.on("error", () => resolve(127));
				child.on("close", (c) => resolve(c ?? 1));
			});

			clearInterval(poll);
			clearInterval(tick);
			signal?.removeEventListener("abort", onAbort);
			drain(); // final flush
			rmSync(eventsDir, { recursive: true, force: true });
			finalStates.set(toolCallId, { state, done: true });
			if (ctx.hasUI) ctx.ui.setStatus(statusKey, undefined);

			const passed = state.passed === true && code === 0;
			let text = summarize(state);
			if (state.passed == null) {
				// Pipeline died before run_end — surface the tail of stderr so the
				// agent can act, rather than a bare exit code.
				const tail = readStderrTail(8);
				text = `greenlight: did not complete (exit ${code}).\n${tail}`;
			}

			if (!passed) {
				// Mark the tool result as an error so the agent treats it as a gate
				// failure to fix, not a success.
				throw new Error(text);
			}
			return {
				content: [{ type: "text", text }],
				details: { state, code },
			};
		},

		renderCall(args, theme) {
			const a = args as Partial<GreenlightRunInput>;
			let s = theme.fg("toolTitle", theme.bold("greenlight "));
			s += theme.fg("muted", "validate branch");
			if (a.intent) {
				const oneLine = a.intent.replace(/\s+/g, " ").trim();
				const preview = oneLine.length > 60 ? `${oneLine.slice(0, 57)}…` : oneLine;
				s += " " + theme.fg("dim", `“${preview}”`);
			}
			return new Text(s, 0, 0);
		},

		renderResult(result, opts, theme, context) {
			const tracked = finalStates.get(context.toolCallId);
			const state =
				(result.details as { state?: PipelineState } | undefined)?.state ?? tracked?.state;
			const text = (context.lastComponent as Text | undefined) ?? new Text("", 0, 0);
			if (!state) {
				// Fallback before any event arrived (or non-state error result).
				const raw = result.content?.[0];
				text.setText(theme.fg("dim", raw && raw.type === "text" ? raw.text : "greenlight…"));
				return text;
			}
			const running = state.passed == null && !tracked?.done;
			// While running, elapsed ticks against wall clock; once done, freeze it to
			// the last event so finished stages don't keep growing on every redraw.
			const now = running ? Math.floor(Date.now() / 1000) : Math.ceil(state.lastTs);
			const lines = renderCard(state, painterFor(theme), {
				now,
				spinner: running ? spinnerFrame() : undefined,
				expanded: opts.expanded,
			});
			// Hint that findings can be expanded, once review has any.
			const hasFindings = state.review.reviewers.some((r) => r.items.length > 0);
			if (hasFindings && !opts.expanded) {
				lines.push(theme.fg("dim", keyHint("app.tools.expand", "to show findings")));
			}
			// Spinner animation is driven by the execute() tick (onUpdate); no
			// self-invalidate here, which would busy-loop the renderer.
			text.setText(lines.join("\n"));
			return text;
		},
	});
}

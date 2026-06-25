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
import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { ExtensionAPI, Theme } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import { Type } from "typebox";

import {
	applyLines,
	initialState,
	type Painter,
	type PipelineState,
	renderCard,
	summarize,
} from "./pipeline-state";

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
	// carries no details.state).
	const finalStates = new Map<string, PipelineState>();

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
			const state = initialState();

			let lastSize = 0;
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
				onUpdate?.({
					content: [{ type: "text", text: summarize(state) }],
					details: { state },
				});
			};

			const child = spawn("greenlight", ["run", "--intent-file", "-"], {
				cwd,
				env: { ...process.env, GREENLIGHT_EVENTS: eventsFile, GREENLIGHT_FORCE_COLOR: "0" },
				stdio: ["pipe", "pipe", "pipe"],
			});
			child.stdin.write(params.intent);
			child.stdin.end();

			let stderr = "";
			child.stderr.on("data", (b: Buffer) => {
				stderr += b.toString();
			});
			// Drain stdout so its pipe never fills and blocks the child; we read
			// progress from the events file, not stdout.
			child.stdout.on("data", () => {});

			const poll = setInterval(drain, 250);
			const onAbort = () => child.kill("SIGTERM");
			signal?.addEventListener("abort", onAbort, { once: true });

			const code: number = await new Promise((resolve) => {
				child.on("error", () => resolve(127));
				child.on("close", (c) => resolve(c ?? 1));
			});

			clearInterval(poll);
			signal?.removeEventListener("abort", onAbort);
			drain(); // final flush
			rmSync(eventsDir, { recursive: true, force: true });
			finalStates.set(toolCallId, state);

			const passed = state.passed === true && code === 0;
			let text = summarize(state);
			if (state.passed == null) {
				// Pipeline died before run_end — surface the tail of stderr so the
				// agent can act, rather than a bare exit code.
				const tail = stderr.trim().split("\n").slice(-8).join("\n");
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

		renderResult(result, _opts, theme, context) {
			const state =
				(result.details as { state?: PipelineState } | undefined)?.state ??
				finalStates.get(context.toolCallId);
			const text = (context.lastComponent as Text | undefined) ?? new Text("", 0, 0);
			if (!state) {
				// Fallback before any event arrived (or non-state error result).
				const raw = result.content?.[0];
				text.setText(theme.fg("dim", raw && raw.type === "text" ? raw.text : "greenlight…"));
				return text;
			}
			text.setText(renderCard(state, painterFor(theme)).join("\n"));
			return text;
		},
	});
}

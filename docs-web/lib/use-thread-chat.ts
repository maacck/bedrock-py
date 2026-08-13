import { useCallback, useEffect, useRef, useState } from "react";
import {
  parseJsonEventStream,
  readUIMessageStream,
  uiMessageChunkSchema,
  type UIMessage,
  type UIMessageChunk,
} from "ai";

const DEVICE_KEY = "bedrock-chat-device-id";
const THREAD_KEY = "bedrock-chat-thread-id";

export type ChatStatus = "idle" | "streaming" | "error";

/** Build the POST /api/chat body for the query+thread_id+device_id contract. */
export function buildChatRequest(
  query: string,
  threadId: string | null,
  deviceId: string,
  location?: string,
): Record<string, unknown> {
  return {
    query,
    thread_id: threadId,
    device_id: deviceId,
    context: location ? { location } : null,
  };
}

/**
 * Replace the LATEST assistant message (the just-added placeholder for the
 * current turn) with each streamed snapshot. Targets the last assistant
 * message so earlier turns' answers are never overwritten; index-based so
 * SDK-generated message ids never mismatch.
 */
export function mergeStreamMessage(messages: UIMessage[], incoming: UIMessage): UIMessage[] {
  const idx = messages.findLastIndex((m) => m.role === "assistant");
  if (idx === -1) return [...messages, incoming];
  const next = [...messages];
  next[idx] = incoming;
  return next;
}

function getDeviceId(): string {
  let id = localStorage.getItem(DEVICE_KEY);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(DEVICE_KEY, id);
  }
  return id;
}

/**
 * Minimal chat client for the query+thread_id+device_id contract.
 * `device_id` is an ownership key only (spoofable, NOT an auth boundary);
 * `thread_id` is server-generated and returned in the X-Thread-Id header.
 */
export function useThreadChat() {
  const [messages, setMessages] = useState<UIMessage[]>([]);
  const [status, setStatus] = useState<ChatStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const threadIdRef = useRef<string | null>(localStorage.getItem(THREAD_KEY));
  const abortRef = useRef<AbortController | null>(null);
  const statusRef = useRef<ChatStatus>("idle");

  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  const startNewThread = useCallback(() => {
    threadIdRef.current = null;
    localStorage.removeItem(THREAD_KEY);
    setMessages([]);
    setError(null);
  }, []);

  const send = useCallback(async (query: string, location?: string) => {
    const trimmed = query.trim();
    if (!trimmed || statusRef.current === "streaming") return;

    const userMessage: UIMessage = {
      id: crypto.randomUUID(),
      role: "user",
      parts: [{ type: "text", text: trimmed }],
    };
    setMessages((prev) => [...prev, userMessage]);
    setStatus("streaming");
    setError(null);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify(buildChatRequest(trimmed, threadIdRef.current, getDeviceId(), location)),
      });

      if (!res.ok) {
        const body = (await res.json().catch(() => null)) as { message?: string } | null;
        throw new Error(body?.message ?? `Request failed (${res.status})`);
      }

      const threadId = res.headers.get("X-Thread-Id");
      if (threadId) {
        threadIdRef.current = threadId;
        localStorage.setItem(THREAD_KEY, threadId);
      }

      setMessages((prev) => [
        ...prev,
        { id: crypto.randomUUID(), role: "assistant", parts: [] },
      ]);

      // Fetch yields Uint8Array SSE bytes; readUIMessageStream wants parsed
      // UIMessageChunk objects. Same decode path as DefaultChatTransport.
      const body = res.body;
      if (!body) {
        throw new Error("Request failed (empty body)");
      }
      const chunkStream: ReadableStream<UIMessageChunk> = parseJsonEventStream({
        stream: body,
        schema: uiMessageChunkSchema,
      }).pipeThrough(
        new TransformStream({
          transform(chunk, controller) {
            if (!chunk.success) {
              throw chunk.error;
            }
            controller.enqueue(chunk.value);
          },
        }),
      );

      for await (const message of readUIMessageStream({
        stream: chunkStream,
      })) {
        setMessages((prev) => mergeStreamMessage(prev, message));
      }
      setStatus("idle");
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        setStatus("idle");
        return;
      }
      setError((err as Error).message);
      setStatus("error");
    }
  }, []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  return { messages, status, error, send, stop, startNewThread };
}

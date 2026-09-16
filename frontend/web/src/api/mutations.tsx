import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api } from "./client";
import type { MutationMethod, PendingMutation } from "./types";
import { createOperationId } from "../domain/library";

interface MutationContextValue {
  pending: PendingMutation | null;
  saving: boolean;
  mutate: <T>(path: string, method: MutationMethod, changes?: Record<string, unknown>) => Promise<T>;
  retry: <T>() => Promise<T>;
  discard: () => void;
}

const MutationContext = createContext<MutationContextValue | null>(null);

export function DurableMutationProvider({
  libraryId,
  children,
}: {
  libraryId: string;
  children: ReactNode;
}) {
  const journalKey = `photo-library-pending:${libraryId}`;
  const initialPending = useMemo(() => {
    try {
      return JSON.parse(localStorage.getItem(journalKey) ?? "null") as PendingMutation | null;
    } catch {
      localStorage.removeItem(journalKey);
      return null;
    }
  }, [journalKey]);
  const [pending, setPending] = useState<PendingMutation | null>(initialPending);
  const [saving, setSaving] = useState(false);
  const pendingRef = useRef<PendingMutation | null>(initialPending);
  const savingRef = useRef(false);

  useEffect(() => {
    try {
      const restored = JSON.parse(localStorage.getItem(journalKey) ?? "null") as PendingMutation | null;
      pendingRef.current = restored;
      setPending(restored);
    } catch {
      localStorage.removeItem(journalKey);
      pendingRef.current = null;
      setPending(null);
    }
  }, [journalKey]);

  const send = useCallback(
    async <T,>(mutation: PendingMutation): Promise<T> => {
      savingRef.current = true;
      setSaving(true);
      try {
        const result = await api.sendMutation<T>(mutation);
        localStorage.removeItem(journalKey);
        pendingRef.current = null;
        setPending(null);
        return result;
      } finally {
        savingRef.current = false;
        setSaving(false);
      }
    },
    [journalKey],
  );

  const mutate = useCallback(
    async <T,>(
      path: string,
      method: MutationMethod,
      changes: Record<string, unknown> = {},
    ): Promise<T> => {
      if (pendingRef.current) throw new Error("Retry or discard the pending change first.");
      const mutation: PendingMutation = {
        path,
        method,
        body: { ...changes, operationId: createOperationId() },
      };
      // Never send an idempotent mutation unless its operation ID has survived a durable write.
      localStorage.setItem(journalKey, JSON.stringify(mutation));
      pendingRef.current = mutation;
      setPending(mutation);
      return send<T>(mutation);
    },
    [journalKey, send],
  );

  const retry = useCallback(async <T,>(): Promise<T> => {
    const mutation = pendingRef.current;
    if (!mutation) throw new Error("There is no pending change to retry.");
    if (savingRef.current) throw new Error("The pending change is already being saved.");
    return send<T>(mutation);
  }, [send]);

  const discard = useCallback(() => {
    localStorage.removeItem(journalKey);
    pendingRef.current = null;
    setPending(null);
  }, [journalKey]);

  const value = useMemo(
    () => ({ pending, saving, mutate, retry, discard }),
    [discard, mutate, pending, retry, saving],
  );
  return <MutationContext.Provider value={value}>{children}</MutationContext.Provider>;
}

export function useDurableMutation() {
  const value = useContext(MutationContext);
  if (!value) throw new Error("Durable mutation context is unavailable");
  return value;
}

import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import type { LibraryFilters } from "../api/types";
import { readFilters, writeFilters } from "../domain/library";

export function useLibraryFilters() {
  const [searchParams, setSearchParams] = useSearchParams();
  const serialized = searchParams.toString();
  const filters = useMemo(() => readFilters(new URLSearchParams(serialized)), [serialized]);
  const setFilters = useCallback(
    (next: LibraryFilters | ((current: LibraryFilters) => LibraryFilters), replace = true) => {
      const value = typeof next === "function" ? next(filters) : next;
      setSearchParams(writeFilters(value), { replace });
    },
    [filters, setSearchParams],
  );
  return [filters, setFilters] as const;
}

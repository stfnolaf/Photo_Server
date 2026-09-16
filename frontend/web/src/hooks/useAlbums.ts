import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

export function useAlbums() {
  const active = useQuery({
    queryKey: ["albums", "active"],
    queryFn: ({ signal }) => api.albums(false, signal),
  });
  const deleted = useQuery({
    queryKey: ["albums", "deleted"],
    queryFn: ({ signal }) => api.albums(true, signal),
  });
  return {
    active: active.data ?? [],
    deleted: deleted.data ?? [],
    all: [...(active.data ?? []), ...(deleted.data ?? [])],
    isLoading: active.isLoading || deleted.isLoading,
    error: active.error ?? deleted.error,
  };
}

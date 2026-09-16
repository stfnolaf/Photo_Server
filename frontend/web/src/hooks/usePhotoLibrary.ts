import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { LibraryFilters } from "../api/types";

export function usePhotoLibrary(filters: LibraryFilters) {
  const query = useInfiniteQuery({
    queryKey: ["library", filters],
    queryFn: ({ pageParam, signal }) => api.browse(filters, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor ?? undefined,
  });
  return {
    ...query,
    photos: query.data?.pages.flatMap((page) => page.items) ?? [],
    total: query.data?.pages[0]?.total ?? 0,
  };
}

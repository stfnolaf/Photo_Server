import { ChevronDown, Search, SlidersHorizontal, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { LibraryFilters } from "../../api/types";
import { Button } from "../../components/Button";
import { useLayoutStore } from "../../state/layout";

export function LibraryToolbar({
  filters,
  setFilters,
}: {
  filters: LibraryFilters;
  setFilters: (filters: LibraryFilters) => void;
}) {
  const [query, setQuery] = useState(filters.q);
  const filtersOpen = useLayoutStore((state) => state.filtersOpen);
  const toggleFilters = useLayoutStore((state) => state.toggleFilters);

  useEffect(() => setQuery(filters.q), [filters.q]);
  useEffect(() => {
    if (query === filters.q) return;
    const timer = window.setTimeout(() => setFilters({ ...filters, q: query }), 300);
    return () => window.clearTimeout(timer);
  }, [filters, query, setFilters]);

  const filterCount = [filters.dateFrom, filters.dateTo, filters.mediaType, filters.ratingMin].filter(Boolean).length;
  const clear = () =>
    setFilters({ ...filters, q: "", dateFrom: "", dateTo: "", mediaType: "", ratingMin: 0 });

  return (
    <div className="library-tools">
      <div className="library-tools__main">
        <label className="search-field">
          <Search size={16} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} type="search" maxLength={200} placeholder="Search scenes, objects, photographs…" aria-label="Search library" />
          {query && <button type="button" onClick={() => setQuery("")} aria-label="Clear search"><X size={14} /></button>}
        </label>
        <Button compact tone={filterCount ? "default" : "ghost"} onClick={toggleFilters} aria-expanded={filtersOpen}>
          <SlidersHorizontal size={14} /> Filters {filterCount > 0 && <span className="filter-count">{filterCount}</span>}
        </Button>
        <label className="sort-control">
          <span className="sr-only">Sort photographs</span>
          <select value={filters.sort} onChange={(event) => setFilters({ ...filters, sort: event.target.value as LibraryFilters["sort"] })}>
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
          <ChevronDown size={13} />
        </label>
      </div>
      {filtersOpen && (
        <div className="filter-drawer">
          <label className="field"><span>From</span><input type="date" value={filters.dateFrom} onChange={(event) => setFilters({ ...filters, dateFrom: event.target.value })} /></label>
          <label className="field"><span>Through</span><input type="date" value={filters.dateTo} onChange={(event) => setFilters({ ...filters, dateTo: event.target.value })} /></label>
          <label className="field"><span>Format</span><select value={filters.mediaType} onChange={(event) => setFilters({ ...filters, mediaType: event.target.value as LibraryFilters["mediaType"] })}><option value="">All formats</option><option value="RAW">RAW</option><option value="JPEG">JPEG</option><option value="HEIF">HEIF</option></select></label>
          <label className="field"><span>Minimum rating</span><select value={filters.ratingMin} onChange={(event) => setFilters({ ...filters, ratingMin: Number(event.target.value) })}><option value={0}>Any rating</option><option value={1}>★ and up</option><option value={2}>★★ and up</option><option value={3}>★★★ and up</option><option value={4}>★★★★ and up</option><option value={5}>★★★★★</option></select></label>
          {(filterCount > 0 || filters.q) && <Button compact tone="ghost" onClick={clear}><X size={13} /> Clear all</Button>}
        </div>
      )}
    </div>
  );
}

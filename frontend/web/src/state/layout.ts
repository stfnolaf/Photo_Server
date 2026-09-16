import { create } from "zustand";
import { persist } from "zustand/middleware";

interface LayoutState {
  thumbnailSize: number;
  leftPanelOpen: boolean;
  inspectorOpen: boolean;
  filtersOpen: boolean;
  setThumbnailSize: (value: number) => void;
  toggleLeftPanel: () => void;
  toggleInspector: () => void;
  toggleFilters: () => void;
}

export const useLayoutStore = create<LayoutState>()(
  persist(
    (set) => ({
      thumbnailSize: 190,
      leftPanelOpen: true,
      inspectorOpen: true,
      filtersOpen: false,
      setThumbnailSize: (thumbnailSize) => set({ thumbnailSize }),
      toggleLeftPanel: () => set((state) => ({ leftPanelOpen: !state.leftPanelOpen })),
      toggleInspector: () => set((state) => ({ inspectorOpen: !state.inspectorOpen })),
      toggleFilters: () => set((state) => ({ filtersOpen: !state.filtersOpen })),
    }),
    { name: "photo-library-layout" },
  ),
);

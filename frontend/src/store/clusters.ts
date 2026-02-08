import { create } from 'zustand';
import { Cluster } from '@/types/api';
import * as api from '@/services/api';

interface ClustersState {
  clusters: Cluster[];
  isLoading: boolean;
  error: string | null;

  fetchClusters: () => Promise<void>;
}

export const useClustersStore = create<ClustersState>((set) => ({
  clusters: [],
  isLoading: false,
  error: null,

  fetchClusters: async () => {
    set({ isLoading: true, error: null });
    try {
      const clusters = await api.getClusters();
      set({ clusters: Array.isArray(clusters) ? clusters : [], isLoading: false });
    } catch (error) {
      set({ error: (error as Error).message, isLoading: false, clusters: [] });
    }
  },
}));

import { useCallback, useState } from 'react'

import { HardDrive, RefreshCw } from 'lucide-react'

import { CacheImageListPanel } from '@/components/local-cache-image-browser'
import {
  formatLocalCacheBytes,
  formatLocalCacheCleanupDescription,
  LOCAL_CACHE_IMAGE_PAGE_SIZE,
  type ImageDateFilters,
} from '@/components/local-cache-image-utils'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useToast } from '@/hooks/use-toast'
import {
  cleanupLocalCache,
  deleteLocalCacheImage,
  deleteLocalCacheImagesByDateRange,
  deleteLocalCacheImagesOlderThanRecentDays,
  getLocalCacheImages,
  type LocalCacheImageItem,
  type LocalCacheImageListResponse,
  type LocalCacheImageTarget,
} from '@/lib/system-api'

const EMOJI_CACHE_TARGET: LocalCacheImageTarget = 'emoji'

interface EmojiCacheMaintenancePanelProps {
  onCacheChanged: () => void
}

export function EmojiCacheMaintenancePanel({ onCacheChanged }: EmojiCacheMaintenancePanelProps) {
  const { toast } = useToast()
  const [open, setOpen] = useState(false)
  const [cacheList, setCacheList] = useState<LocalCacheImageListResponse | null>(null)
  const [cachePage, setCachePage] = useState(1)
  const [filters, setFilters] = useState<ImageDateFilters>({ endDate: '', startDate: '' })
  const [isLoading, setIsLoading] = useState(false)
  const [isCleaning, setIsCleaning] = useState(false)
  const [deletingKey, setDeletingKey] = useState<string | null>(null)

  const refreshCacheList = useCallback(async (page: number, nextFilters = filters) => {
    setIsLoading(true)
    try {
      const result = await getLocalCacheImages({
        target: EMOJI_CACHE_TARGET,
        page,
        page_size: LOCAL_CACHE_IMAGE_PAGE_SIZE,
        start_date: nextFilters.startDate || undefined,
        end_date: nextFilters.endDate || undefined,
      })
      setCacheList(result)
    } catch (error) {
      toast({
        title: '获取表情包缓存失败',
        description: error instanceof Error ? error.message : '请稍后重试',
        variant: 'destructive',
      })
    } finally {
      setIsLoading(false)
    }
  }, [filters, toast])

  const refreshCurrentPage = useCallback(async () => {
    await refreshCacheList(cachePage)
  }, [cachePage, refreshCacheList])

  const notifyChanged = useCallback(() => {
    onCacheChanged()
  }, [onCacheChanged])

  const handleOpenChange = (nextOpen: boolean) => {
    setOpen(nextOpen)
    if (nextOpen && !cacheList) {
      void refreshCacheList(cachePage)
    }
  }

  const handlePageChange = (_target: LocalCacheImageTarget, page: number) => {
    setCachePage(page)
    void refreshCacheList(page)
  }

  const handleFilterChange = (_target: LocalCacheImageTarget, nextFilters: ImageDateFilters) => {
    setFilters(nextFilters)
  }

  const handleFilterSubmit = (_target: LocalCacheImageTarget, nextFilters = filters) => {
    setCachePage(1)
    void refreshCacheList(1, nextFilters)
  }

  const handleFilterClear = () => {
    const nextFilters = { endDate: '', startDate: '' }
    setFilters(nextFilters)
    setCachePage(1)
    void refreshCacheList(1, nextFilters)
  }

  const handleDeleteAll = async () => {
    setIsCleaning(true)
    try {
      const result = await cleanupLocalCache(EMOJI_CACHE_TARGET)
      setCachePage(1)
      await refreshCacheList(1)
      notifyChanged()
      toast({
        title: result.message,
        description: formatLocalCacheCleanupDescription(result),
      })
    } catch (error) {
      toast({
        title: '清理表情包缓存失败',
        description: error instanceof Error ? error.message : '请稍后重试',
        variant: 'destructive',
      })
    } finally {
      setIsCleaning(false)
    }
  }

  const handleDeleteDateRange = async () => {
    setIsCleaning(true)
    try {
      const result = await deleteLocalCacheImagesByDateRange(EMOJI_CACHE_TARGET, filters.startDate, filters.endDate)
      setCachePage(1)
      await refreshCacheList(1, filters)
      notifyChanged()
      toast({
        title: result.message,
        description: formatLocalCacheCleanupDescription(result),
      })
    } catch (error) {
      toast({
        title: '按日期删除表情包缓存失败',
        description: error instanceof Error ? error.message : '请稍后重试',
        variant: 'destructive',
      })
    } finally {
      setIsCleaning(false)
    }
  }

  const handleDeleteOlderThanRecentDays = async (_target: LocalCacheImageTarget, days: 1 | 7 | 30) => {
    setIsCleaning(true)
    try {
      const result = await deleteLocalCacheImagesOlderThanRecentDays(EMOJI_CACHE_TARGET, days)
      setCachePage(1)
      await refreshCacheList(1)
      notifyChanged()
      toast({
        title: result.message,
        description: formatLocalCacheCleanupDescription(result),
      })
    } catch (error) {
      toast({
        title: '清理旧表情包缓存失败',
        description: error instanceof Error ? error.message : '请稍后重试',
        variant: 'destructive',
      })
    } finally {
      setIsCleaning(false)
    }
  }

  const handleDeleteCacheImage = async (_target: LocalCacheImageTarget, item: LocalCacheImageItem) => {
    const itemKey = `${EMOJI_CACHE_TARGET}:${item.relative_path}`
    setDeletingKey(itemKey)
    try {
      const result = await deleteLocalCacheImage(EMOJI_CACHE_TARGET, item.relative_path)
      const remainingTotal = Math.max(0, (cacheList?.total ?? 1) - 1)
      const nextPage = Math.max(1, Math.min(cachePage, Math.ceil(remainingTotal / LOCAL_CACHE_IMAGE_PAGE_SIZE) || 1))
      setCachePage(nextPage)
      await refreshCacheList(nextPage)
      notifyChanged()
      toast({
        title: result.message,
        description: formatLocalCacheCleanupDescription(result),
      })
    } catch (error) {
      toast({
        title: '删除表情包缓存失败',
        description: error instanceof Error ? error.message : '请稍后重试',
        variant: 'destructive',
      })
    } finally {
      setDeletingKey(null)
    }
  }

  const cleanupDisabled = isCleaning || isLoading || deletingKey !== null
  const total = cacheList?.total ?? 0
  const totalSize = cacheList?.total_size ?? 0

  return (
    <>
      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <CardTitle className="flex items-center gap-2 text-base">
                <HardDrive className="h-4 w-4" />
                表情包缓存维护
              </CardTitle>
              <CardDescription className="mt-1">
                按文件和日期维护表情包缓存，处理仅文件、旧缓存和磁盘占用。
              </CardDescription>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={refreshCurrentPage}
                disabled={isLoading}
                className="gap-2"
              >
                <RefreshCw className={`h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
                刷新缓存
              </Button>
              <Button size="sm" onClick={() => handleOpenChange(true)}>
                打开缓存维护
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <div className="text-xs text-muted-foreground">缓存文件</div>
              <div className="text-lg font-semibold">{total}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">占用空间</div>
              <div className="text-lg font-semibold">{formatLocalCacheBytes(totalSize)}</div>
            </div>
          </div>
        </CardContent>
      </Card>

      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent className="[--dialog-width:72rem]">
          <DialogHeader>
            <DialogTitle>表情包缓存维护</DialogTitle>
            <DialogDescription>
              按日期浏览缓存文件，支持单个删除、区间删除和清理最近指定天数以外的数据。
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            <CacheImageListPanel
              target={EMOJI_CACHE_TARGET}
              list={cacheList}
              filters={filters}
              isLoading={isLoading}
              deletingKey={deletingKey}
              cleanupDisabled={cleanupDisabled}
              onRefresh={() => void refreshCurrentPage()}
              onPageChange={handlePageChange}
              onDelete={handleDeleteCacheImage}
              onDeleteAll={handleDeleteAll}
              onFilterChange={handleFilterChange}
              onFilterSubmit={handleFilterSubmit}
              onFilterClear={handleFilterClear}
              onDeleteDateRange={handleDeleteDateRange}
              onDeleteOlderThanRecentDays={handleDeleteOlderThanRecentDays}
            />
          </DialogBody>
        </DialogContent>
      </Dialog>
    </>
  )
}

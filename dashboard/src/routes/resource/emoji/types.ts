// 上传文件的元数据类型
export interface UploadedFileInfo {
  id: string
  name: string
  previewUrl: string
  tags: string[]
  file: File
}

// 上传步骤类型
export type UploadStep = 'select' | 'edit-single' | 'edit-multiple'

import { uploadAttachment, type AttachmentInfo } from './api'

const IMAGE_LIMIT = 5 * 1024 * 1024  // the server's limit for one image
const FILE_LIMIT = 20 * 1024 * 1024
const LONG_EDGE = 2576  // larger photos are scaled down: the models see no more detail than this

/**
 * Phone photos are often larger than models accept (and HEIC on iPhones). Redraw them as JPEG with a bounded
 * size; anything the browser can't decode is uploaded as it is and the server decides.
 */
async function prepare(file: File): Promise<{ blob: Blob; name: string }> {
  const heic = /^image\/hei[cf]$/i.test(file.type) || /\.hei[cf]$/i.test(file.name)
  const photo = /^image\/(jpeg|png|webp)$/i.test(file.type)
  // Small images aren't worth decoding: they are within every limit already
  if (!heic && !(photo && file.size > 1024 * 1024)) return { blob: file, name: file.name }
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file)
  } catch {
    return { blob: file, name: file.name }
  }
  const scale = Math.min(1, LONG_EDGE / Math.max(bitmap.width, bitmap.height))
  if (scale === 1 && !heic && file.size <= IMAGE_LIMIT * 0.9) { bitmap.close(); return { blob: file, name: file.name } }
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(bitmap.width * scale))
  canvas.height = Math.max(1, Math.round(bitmap.height * scale))
  const context = canvas.getContext('2d')!
  context.fillStyle = '#fff'  // JPEG has no transparency
  context.fillRect(0, 0, canvas.width, canvas.height)
  context.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
  bitmap.close()
  for (const quality of [0.9, 0.8, 0.7, 0.6]) {
    const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality))
    if (blob && blob.size <= IMAGE_LIMIT * 0.95) return { blob, name: file.name.replace(/\.[^.]*$/, '') + '.jpg' }
  }
  return { blob: file, name: file.name }
}

export async function attach(file: File): Promise<AttachmentInfo> {
  if (file.size > FILE_LIMIT && !file.type.startsWith('image/')) throw new Error('添付できるファイルは20 MiBまでです')
  const { blob, name } = await prepare(file)
  return uploadAttachment(blob, name || 'file')
}

export const sizeLabel = (size: number) => size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MiB`
  : size >= 1024 ? `${Math.round(size / 1024)} KiB` : `${size} B`

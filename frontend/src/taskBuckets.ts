export type TaskBucket = 'progress'|'done'|'filtered'|'unidentified'|'failed'|'cancelled'

export function taskBucket(job:any):TaskBucket {
  if(['queued','running','paused','waiting_login'].includes(job.state)) return 'progress'
  if(job.state==='cancelled') return 'cancelled'
  if(['needs_review','partial','failed'].includes(job.state)) return 'failed'
  const counts=job.counts||{}
  if((job.unidentified||0)>0||(counts.pending||0)>0) return 'unidentified'
  if((job.filtered||0)>0) return 'filtered'
  return 'done'
}

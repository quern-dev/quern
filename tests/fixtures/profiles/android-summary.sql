
select
  (select start_ts from trace_bounds) as start_ts,
  (select end_ts from trace_bounds) as end_ts,
  (select count(*) from process where name = 'com.quern.probe') as procs,
  hex((select json_group_array(json_array(track, ts, value)) from (
     select t.name as track, c.ts as ts, c.value as value
     from counter c join counter_track t on c.track_id = t.id
     where t.name like 'wq:%' order by t.name, c.ts))) as wq,
  hex((select json_group_array(s.dur) from slice s
     join thread_track tt on s.track_id = tt.id join thread t using (utid)
     join process p using (upid)
     where p.name = 'com.quern.probe' and t.tid = p.pid
       and (s.name = 'Choreographer#doFrame' or s.name glob 'Choreographer#doFrame [0-9]*')))
     as frames,
  hex((select json_group_array(json_array(tid, thread, ns)) from (
     select t.tid as tid, t.name as thread, sum(s.dur) as ns
     from sched s join thread t using (utid) join process p using (upid)
     where p.name = 'com.quern.probe' group by utid order by ns desc))) as cpu,
  hex((select json_group_array(json_array(name, thread, dur, ts)) from (
     select s.name as name, t.name as thread, s.dur as dur, s.ts as ts
     from slice s join thread_track tt on s.track_id = tt.id
     join thread t using (utid) join process p using (upid)
     where p.name = 'com.quern.probe' order by s.dur desc limit 10))) as slices,
  hex((select json_group_array(json_array(name, idx, value)) from stats
     where severity = 'data_loss' and value > 0)) as loss


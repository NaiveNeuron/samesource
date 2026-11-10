-- Example queries for the release database (sqlite3 output/dataset/pkgxdist.sqlite < examples/queries.sql).

-- 1. Packages in different distributions that build from the same upstream bytes
--    under different names (first 10; name_key drops Gentoo categories).
SELECT DISTINCT ra.family, ra.source_package, rb.family, rb.source_package, aa.name AS archive
FROM links l
JOIN artifacts aa ON aa.id = l.a JOIN records ra ON ra.id = aa.record_id
JOIN artifacts ab ON ab.id = l.b JOIN records rb ON rb.id = ab.record_id
WHERE l.role_a = 'primary' AND l.role_b = 'primary' AND ra.name_key <> rb.name_key
ORDER BY ra.source_package LIMIT 10;

-- 2. Byte-matched translation tasks from Debian to Fedora, with the pinned
--    packaging state of both sides (first 5 test-split tasks).
SELECT t.task_id, s.packaging_repository || ' ' || coalesce(s.packaging_path, '') AS source_state,
       d.packaging_repository || '@' || d.packaging_commit AS target_state
FROM tasks t JOIN records s ON s.id = t.source_id JOIN records d ON d.id = t.target_id
WHERE t.source_family = 'deb' AND t.target_family = 'rpm' AND t.split = 'test'
ORDER BY t.task_id LIMIT 5;

-- 3. How many archive groups span each combination of distributions.
SELECT families, COUNT(*) AS groups FROM groups GROUP BY families ORDER BY groups DESC;

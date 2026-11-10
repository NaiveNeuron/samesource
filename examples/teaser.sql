-- The two cases in Figure 1, checkable against the release
-- (sqlite3 samesource.sqlite < examples/teaser.sql).

-- (a) allegro: four names, one group, one primary archive (Arch declares
--     SHA-256 only and links to Debian's SHA-256).
SELECT r.id, a.name, d.algorithm, substr(d.digest, 1, 16) AS digest_prefix, d.source
FROM group_members gm
JOIN records r ON r.id = gm.record_id
JOIN artifacts a ON a.record_id = r.id AND a.role = 'primary'
JOIN digests d ON d.artifact_id = a.id AND d.algorithm IN ('sha512', 'sha256')
WHERE gm.group_id = (SELECT group_id FROM group_members
                     WHERE record_id = 'pkgbuild:allegro4@4.4.3.1-5')
ORDER BY r.family, d.algorithm;

-- (b) transmission 4.1.4: Debian's repacked .orig differs from the release
--     tarball that Fedora, Arch, and Gentoo share.
SELECT r.id, a.name, d.algorithm, substr(d.digest, 1, 16) AS digest_prefix, d.source
FROM records r
JOIN artifacts a ON a.record_id = r.id AND a.role = 'primary'
JOIN digests d ON d.artifact_id = a.id AND d.algorithm IN ('sha512', 'sha256')
WHERE r.name_key = 'transmission' AND r.upstream_version LIKE '4.1.4%'
ORDER BY r.family, d.algorithm;

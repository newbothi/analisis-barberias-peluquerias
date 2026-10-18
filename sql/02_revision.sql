/*
    analisis-barberias-peluquerias — Fase 1
    Consultas para revisar el resultado de la extracción.
*/
USE AnalisisBarberiasPeluquerias;
GO

-- 1. Resumen de las corridas
SELECT TOP (10) EjecucionId, InicioUtc, FinUtc, CuentasOk, CuentasError, PostsGuardados, Parametros
FROM dbo.Ejecuciones
ORDER BY EjecucionId DESC;

-- 2. Cuentas que fallaron y por qué
SELECT Username, Nombre, EstadoLista, MensajeError
FROM dbo.Cuentas
WHERE EstadoExtraccion = N'error'
ORDER BY Username;

-- 3. Cobertura por cuenta: cuántos posts, desde cuándo y cuán activa está
SELECT
    c.Username,
    c.Nombre,
    c.Comuna,
    c.EstadoLista,
    c.Seguidores,
    COUNT(p.PostId)                                         AS Posts,
    MIN(p.FechaPublicacionUtc)                              AS PrimerPost,
    MAX(p.FechaPublicacionUtc)                              AS UltimoPost,
    DATEDIFF(DAY, MAX(p.FechaPublicacionUtc), SYSUTCDATETIME()) AS DiasSinPublicar,
    SUM(CASE WHEN p.Likes IS NULL THEN 1 ELSE 0 END)        AS PostsSinLikes
FROM dbo.Cuentas c
LEFT JOIN dbo.Posts p ON p.Username = c.Username
WHERE c.EstadoExtraccion = N'ok'
GROUP BY c.Username, c.Nombre, c.Comuna, c.EstadoLista, c.Seguidores
ORDER BY Posts DESC;

-- 4. Candidatas a descartar en la Fase 2: pocos posts en el último año o inactivas hace más de 60 días
SELECT Username, Posts, DiasSinPublicar
FROM (
    SELECT
        c.Username,
        SUM(CASE WHEN p.FechaPublicacionUtc >= DATEADD(MONTH, -12, SYSUTCDATETIME()) THEN 1 ELSE 0 END) AS Posts,
        DATEDIFF(DAY, MAX(p.FechaPublicacionUtc), SYSUTCDATETIME()) AS DiasSinPublicar
    FROM dbo.Cuentas c
    LEFT JOIN dbo.Posts p ON p.Username = c.Username
    WHERE c.EstadoExtraccion = N'ok'
    GROUP BY c.Username
) x
WHERE Posts < 30 OR DiasSinPublicar > 60 OR DiasSinPublicar IS NULL
ORDER BY Posts;

-- 5. Totales por formato
SELECT MediaType, MediaProductType, COUNT(*) AS Posts, AVG(CAST(Likes AS FLOAT)) AS LikesPromedio
FROM dbo.Posts
GROUP BY MediaType, MediaProductType
ORDER BY Posts DESC;

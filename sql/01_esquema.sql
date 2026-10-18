/*
    analisis-barberias-peluquerias — Fase 1
    Esquema de la base de datos para la extracción de Instagram (Business Discovery).

    Ejecutar una sola vez en SSMS. Es idempotente: si la base o las tablas ya existen, no las toca.
*/

IF DB_ID(N'AnalisisBarberiasPeluquerias') IS NULL
    CREATE DATABASE AnalisisBarberiasPeluquerias;
GO

USE AnalisisBarberiasPeluquerias;
GO

/* Una fila por barbería de la lista, con el resultado de su última extracción. */
IF OBJECT_ID(N'dbo.Cuentas', N'U') IS NULL
CREATE TABLE dbo.Cuentas (
    Username            NVARCHAR(100)  NOT NULL CONSTRAINT PK_Cuentas PRIMARY KEY,
    Nombre              NVARCHAR(200)  NULL,
    Comuna              NVARCHAR(100)  NULL,
    EstadoLista         NVARCHAR(30)   NULL,      -- confirmado_en_fuente | encontrado | dudoso (viene del CSV)
    IgId                NVARCHAR(50)   NULL,      -- id de la cuenta en Instagram
    Seguidores          INT            NULL,
    CantidadPosts       INT            NULL,      -- total de posts que tiene la cuenta
    PostsExtraidos      INT            NULL,      -- posts traídos en la última extracción
    EstadoExtraccion    NVARCHAR(20)   NOT NULL,  -- ok | error
    MensajeError        NVARCHAR(1000) NULL,
    UltimaExtraccionUtc DATETIME2(0)   NOT NULL
);
GO

/* Seguidores en cada corrida: permite ver crecimiento en el tiempo (sirve para las fases 6 y 7). */
IF OBJECT_ID(N'dbo.CuentasSnapshot', N'U') IS NULL
CREATE TABLE dbo.CuentasSnapshot (
    Id              BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_CuentasSnapshot PRIMARY KEY,
    Username        NVARCHAR(100) NOT NULL CONSTRAINT FK_CuentasSnapshot_Cuentas REFERENCES dbo.Cuentas(Username),
    FechaCapturaUtc DATETIME2(0)  NOT NULL,
    Seguidores      INT           NOT NULL,
    CantidadPosts   INT           NOT NULL
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_CuentasSnapshot_Username_Fecha')
    CREATE INDEX IX_CuentasSnapshot_Username_Fecha ON dbo.CuentasSnapshot (Username, FechaCapturaUtc);
GO

/* Un post por fila. Si se vuelve a extraer, se actualizan likes, comentarios y caption. */
IF OBJECT_ID(N'dbo.Posts', N'U') IS NULL
CREATE TABLE dbo.Posts (
    PostId              NVARCHAR(50)  NOT NULL CONSTRAINT PK_Posts PRIMARY KEY,
    Username            NVARCHAR(100) NOT NULL CONSTRAINT FK_Posts_Cuentas REFERENCES dbo.Cuentas(Username),
    Caption             NVARCHAR(MAX) NULL,
    FechaPublicacionUtc DATETIME2(0)  NOT NULL,
    MediaType           NVARCHAR(30)  NULL,       -- IMAGE | VIDEO | CAROUSEL_ALBUM
    MediaProductType    NVARCHAR(30)  NULL,       -- FEED | REELS
    Likes               INT           NULL,       -- NULL si la cuenta oculta los likes
    Comentarios         INT           NULL,
    Permalink           NVARCHAR(300) NULL,
    FechaCapturaUtc     DATETIME2(0)  NOT NULL    -- cuándo se leyeron los likes (para calcular la edad del post)
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_Posts_Username_Fecha')
    CREATE INDEX IX_Posts_Username_Fecha ON dbo.Posts (Username, FechaPublicacionUtc);
GO

/* Respuesta JSON original de cada página, por si después se necesita un campo que no se guardó. */
IF OBJECT_ID(N'dbo.RespuestasCrudas', N'U') IS NULL
CREATE TABLE dbo.RespuestasCrudas (
    Id              BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_RespuestasCrudas PRIMARY KEY,
    EjecucionId     INT           NULL,
    Username        NVARCHAR(100) NOT NULL,
    Pagina          INT           NOT NULL,
    FechaCapturaUtc DATETIME2(0)  NOT NULL,
    Json            NVARCHAR(MAX) NOT NULL
);
GO

/* Resumen de cada corrida del script. */
IF OBJECT_ID(N'dbo.Ejecuciones', N'U') IS NULL
CREATE TABLE dbo.Ejecuciones (
    EjecucionId     INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Ejecuciones PRIMARY KEY,
    InicioUtc       DATETIME2(0)  NOT NULL,
    FinUtc          DATETIME2(0)  NULL,
    CuentasOk       INT           NULL,
    CuentasError    INT           NULL,
    PostsGuardados  INT           NULL,
    Parametros      NVARCHAR(500) NULL
);
GO

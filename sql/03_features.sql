/*
    analisis-barberias-peluquerias — Fase 2
    Tablas con los posts limpios y sus features, listas para modelar.

    Ejecutar una sola vez en SSMS. Es idempotente.
    El script preparar_features.py vacía y vuelve a llenar estas tablas en cada corrida.
*/
USE AnalisisBarberiasPeluquerias;
GO

/* Un post válido por fila: dentro de la ventana, con al menos 7 días, con likes visibles y sin sorteos. */
IF OBJECT_ID(N'dbo.PostsFeatures', N'U') IS NULL
CREATE TABLE dbo.PostsFeatures (
    PostId                NVARCHAR(50)   NOT NULL CONSTRAINT PK_PostsFeatures PRIMARY KEY,
    Username              NVARCHAR(100)  NOT NULL,
    FechaPublicacionUtc   DATETIME2(0)   NOT NULL,
    FechaLocal            DATETIME2(0)   NOT NULL,   -- hora de Chile (America/Santiago)
    Anio                  SMALLINT       NOT NULL,
    Mes                   TINYINT        NOT NULL,
    DiaSemana             TINYINT        NOT NULL,   -- 0 = lunes ... 6 = domingo
    NombreDia             NVARCHAR(10)   NOT NULL,
    EsFinDeSemana         BIT            NOT NULL,
    HoraLocal             TINYINT        NOT NULL,
    Franja                NVARCHAR(12)   NOT NULL,   -- madrugada | manana | tarde | noche
    Formato               NVARCHAR(12)   NOT NULL,   -- reel | carrusel | imagen | video_feed
    TextoPropio           NVARCHAR(2200) NULL,       -- caption sin la plantilla que la cuenta repite
    LargoTextoPropio      INT            NOT NULL,
    PalabrasTextoPropio   INT            NOT NULL,
    HashtagsPropios       INT            NOT NULL,
    HashtagsTotal         INT            NOT NULL,
    EmojisPropios         INT            NOT NULL,
    TienePregunta         BIT            NOT NULL,
    TieneLlamadoAccion    BIT            NOT NULL,   -- agenda, reserva, escríbenos, link en bio...
    MencionaPrecioPromo   BIT            NOT NULL,
    DiasDesdePostAnterior FLOAT          NULL,       -- NULL en el primer post conocido de la cuenta
    PostsUltimos7Dias     INT            NOT NULL,   -- posts de la cuenta en los 7 días previos
    Seguidores            INT            NOT NULL,
    LogSeguidores         FLOAT          NOT NULL,
    EdadDiasCaptura       FLOAT          NOT NULL,   -- días entre la publicación y la lectura de likes
    Likes                 INT            NOT NULL,
    Comentarios           INT            NULL,
    LogLikes              FLOAT          NOT NULL,   -- target: ln(1 + likes)
    LogComentarios        FLOAT          NULL,
    EngagementRate        FLOAT          NOT NULL,   -- (likes + comentarios) / seguidores
    PesoCuenta            FLOAT          NOT NULL,   -- equilibra cuentas con muchos posts (promedio = 1)
    FechaGeneracionUtc    DATETIME2(0)   NOT NULL
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_PostsFeatures_Username_Fecha')
    CREATE INDEX IX_PostsFeatures_Username_Fecha ON dbo.PostsFeatures (Username, FechaPublicacionUtc);
GO

/* Una fila por cuenta: cuántos posts pasaron cada filtro y si quedó incluida en el dataset. */
IF OBJECT_ID(N'dbo.CuentasResumen', N'U') IS NULL
CREATE TABLE dbo.CuentasResumen (
    Username            NVARCHAR(100) NOT NULL CONSTRAINT PK_CuentasResumen PRIMARY KEY,
    EstadoLista         NVARCHAR(30)  NULL,
    Seguidores          INT           NULL,
    PostsExtraidos      INT           NOT NULL,
    PostsEnVentana      INT           NOT NULL,
    PostsMuyRecientes   INT           NOT NULL,
    PostsSinLikes       INT           NOT NULL,
    Sorteos             INT           NOT NULL,
    PostsValidos        INT           NOT NULL,
    LineasPlantilla     INT           NOT NULL,
    DiasSinPublicar     INT           NULL,
    Incluida            BIT           NOT NULL,
    MotivoExclusion     NVARCHAR(200) NULL,
    FechaGeneracionUtc  DATETIME2(0)  NOT NULL
);
GO

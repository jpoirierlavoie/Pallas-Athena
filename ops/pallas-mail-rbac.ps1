<#
    Pallas Athéna — portée Exchange de la boîte du juriste (lot messagerie).

    À exécuter par un compte disposant de :
      * Exchange Administrator (ou membre d'Organization Management)  -> étapes 2 à 5
      * rien d'autre : ce script NE TOUCHE PAS Microsoft Entra, et c'est le point.

    ─────────────────────────────────────────────────────────────────────────
    LE POINT LE PLUS IMPORTANT, ET IL EST CONTRE-INTUITIF
    ─────────────────────────────────────────────────────────────────────────
    NE PAS accorder « Mail.ReadWrite » dans Entra.

    Les deux systèmes s'ADDITIONNENT. La documentation Microsoft le dit sans
    ambiguïté : une permission accordée à l'échelle de l'organisation dans
    Entra, PLUS la même permission portée dans Exchange RBAC, donne « no
    effective resource scoping » — l'application obtient la permission sur
    TOUTES les boîtes du locataire et la portée ne sert à rien.

    Donc : Mail.ReadWrite s'accorde ICI, dans Exchange, et NULLE PART ailleurs.
    Si quelqu'un l'ajoute un jour dans Entra « pour que ça marche », la portée
    de ce script cesse silencieusement de porter quoi que ce soit.

    Référence : https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac
    (section FAQ, « Why does my application still have access to mailboxes
    that aren't granted by the scope I used »)
#>

$ErrorActionPreference = 'Stop'

# ── Valeurs du cabinet ──────────────────────────────────────────────────────
$AppId       = '988bf117-3aef-463b-8062-7dac226e50d9'   # Pallas-Athena-Graph
$TenantId    = '4c5c39a5-2e63-4b04-8408-973c58cd88c7'
$Mailbox     = 'jason@poirierlavoie.ca'                 # reception@ EN EST UN ALIAS
$ScopeName   = 'Pallas-Athena-Boite-Juriste'
$SpDisplay   = 'Pallas-Athena-Graph'

# Une boîte TIERCE, pour le test négatif — FACULTATIF.
# Le locataire n'en compte qu'une (confirmé 2026-08-28), donc ce test ne peut
# pas être fait : il n'y a rien à exclure. Laisser vide. Le script vérifie
# alors le FILTRE lui-même, qui est ce qui reste de vérifiable ici.
$MailboxTemoin = ''


# ── 1. Modules ──────────────────────────────────────────────────────────────
foreach ($m in @('ExchangeOnlineManagement','Microsoft.Graph.Applications')) {
    if (-not (Get-Module -ListAvailable -Name $m)) {
        Write-Host "Installation de $m ..." -ForegroundColor Cyan
        Install-Module $m -Scope CurrentUser -Force -AllowClobber
    }
}


# ── 2. L'ObjectId du service principal ──────────────────────────────────────
# ⚠ Celui des « Applications d'entreprise », PAS celui des « Inscriptions
# d'applications » : la page des inscriptions affiche un AUTRE identifiant, et
# New-ServicePrincipal le refuse (ou pire, pointe ailleurs).
Connect-MgGraph -TenantId $TenantId -Scopes 'Application.Read.All' -NoWelcome
$sp = Get-MgServicePrincipal -Filter "appId eq '$AppId'"
if (-not $sp) { throw "Aucun service principal pour l'AppId $AppId dans ce locataire." }
$SpObjectId = $sp.Id
Write-Host "Service principal : $($sp.DisplayName)  ObjectId=$SpObjectId" -ForegroundColor Green

# Ce que l'application détient DÉJÀ dans Entra — à lire avant d'aller plus loin.
Write-Host "`nPermissions d'application actuellement accordées dans Entra :" -ForegroundColor Yellow
$graphSp = Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'"
Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $SpObjectId |
    ForEach-Object {
        $role = $graphSp.AppRoles | Where-Object Id -eq $_.AppRoleId
        [pscustomobject]@{ Permission = $role.Value; Portee = 'ORGANISATION ENTIERE' }
    } | Format-Table -AutoSize

Write-Host @"
Si « Mail.ReadWrite » apparait ci-dessus, ARRETEZ : la portee creee plus bas
ne portera rien du tout (les deux systemes s'additionnent). Retirez d'abord la
permission dans Entra, puis relancez ce script.
"@ -ForegroundColor Red


# ── 3. Connexion Exchange ───────────────────────────────────────────────────
Connect-ExchangeOnline -ShowBanner:$false


# ── 4. Le pointeur Exchange vers le service principal ───────────────────────
if (-not (Get-ServicePrincipal -Identity $AppId -ErrorAction SilentlyContinue)) {
    New-ServicePrincipal -AppId $AppId -ObjectId $SpObjectId -DisplayName $SpDisplay
} else {
    Write-Host "Le service principal Exchange existe deja." -ForegroundColor Gray
}


# ── 5. La portée : UNE boîte, et une seule ──────────────────────────────────
if (-not (Get-ManagementScope -Identity $ScopeName -ErrorAction SilentlyContinue)) {
    New-ManagementScope -Name $ScopeName `
        -RecipientRestrictionFilter "PrimarySmtpAddress -eq '$Mailbox'"
} else {
    Write-Host "La portee $ScopeName existe deja." -ForegroundColor Gray
}


# ── 6. Le rôle, porté ───────────────────────────────────────────────────────
# « Application Mail.ReadWrite » = lire, creer, modifier, supprimer un courriel.
# Il N'INCLUT PAS l'envoi — c'est exactement ce qu'on veut : l'application
# depose des brouillons et n'envoie jamais.
$assignmentName = 'Pallas-Athena-Mail-ReadWrite-Boite-Juriste'
if (-not (Get-ManagementRoleAssignment -Identity $assignmentName -ErrorAction SilentlyContinue)) {
    New-ManagementRoleAssignment -Name $assignmentName `
        -App $SpObjectId `
        -Role 'Application Mail.ReadWrite' `
        -CustomResourceScope $ScopeName
} else {
    Write-Host "L'attribution existe deja." -ForegroundColor Gray
}


# ── 7. Vérification — la seule qui compte ───────────────────────────────────
# Test-ServicePrincipalAuthorization CONTOURNE le cache (30 min a 2 h), donc
# c'est le seul moyen de verifier tout de suite.
Write-Host "`n=== La boite visee : InScope doit etre True ===" -ForegroundColor Cyan
Test-ServicePrincipalAuthorization -Identity $AppId -Resource $Mailbox | Format-Table -AutoSize

if ($MailboxTemoin) {
    Write-Host "`n=== Une boite TIERCE : InScope doit etre False ===" -ForegroundColor Cyan
    Write-Host "(lire reception@ REUSSIRA — c'est un ALIAS de la meme boite, pas une autre)" -ForegroundColor Gray
    Test-ServicePrincipalAuthorization -Identity $AppId -Resource $MailboxTemoin | Format-Table -AutoSize
} else {
    # Locataire a UNE seule boite : il n'y a rien a exclure, donc le test
    # negatif est impossible — et le dire est plus utile que de le simuler.
    # Ce qui reste verifiable, c'est le FILTRE : il doit resoudre vers
    # EXACTEMENT un destinataire, et le bon. C'est cela qui fera le travail
    # le jour ou une deuxieme boite apparaitra.
    Write-Host "`n=== Test negatif impossible : une seule boite dans le locataire ===" -ForegroundColor Yellow
    Write-Host "A la place, ce que le filtre de la portee resout :" -ForegroundColor Cyan
    $matches = Get-Recipient -RecipientPreviewFilter "PrimarySmtpAddress -eq '$Mailbox'"
    $matches | Format-Table Name, PrimarySmtpAddress, RecipientType -AutoSize
    if (@($matches).Count -eq 1 -and $matches.PrimarySmtpAddress -eq $Mailbox) {
        Write-Host "OK : le filtre resout vers exactement 1 destinataire, le bon." -ForegroundColor Green
        Write-Host "     Une boite ajoutee plus tard sera HORS de cette portee." -ForegroundColor Green
    } else {
        Write-Host "ATTENTION : le filtre ne resout pas vers la seule boite attendue." -ForegroundColor Red
    }
}


# ── 8. Réglage informatif ───────────────────────────────────────────────────
# Decide si un courriel du portail revient estampille reception@ ou jason@.
# N'affecte PAS la protection du lien de connexion (le retrait du oobCode est
# fait cote application, sur le CONTENU) — explique seulement le compteur
# app_sent_excluded_best_effort.
Write-Host "`nSendFromAliasEnabled :" -ForegroundColor Cyan
Get-OrganizationConfig | Select-Object SendFromAliasEnabled | Format-Table -AutoSize

# Archive en ligne : GET /users/{upn}/messages NE L'ATTEINT PAS.
Write-Host "Archive en ligne de la boite :" -ForegroundColor Cyan
Get-Mailbox -Identity $Mailbox | Select-Object ArchiveStatus, ArchiveName | Format-Table -AutoSize

Write-Host "`nTermine. Ne deployez le code qu'apres un InScope=True ci-dessus." -ForegroundColor Green
Write-Host @"

Ce qui est prouve aujourd'hui, et ce qui ne l'est pas :
  PROUVE     : l'application a Mail.ReadWrite sur jason@ (InScope=True).
  PROUVE     : le filtre de la portee ne designe que cette boite.
  NON PROUVE : qu'elle est REFUSEE ailleurs — le locataire n'a qu'une boite,
               il n'y a donc rien a refuser. La portee prend sa valeur le jour
               ou une deuxieme boite est creee : elle en sera exclue d'office,
               ce qu'un grant Entra n'aurait pas fait.
"@ -ForegroundColor Gray

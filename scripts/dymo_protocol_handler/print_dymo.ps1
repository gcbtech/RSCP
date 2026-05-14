param (
    [string]$Url
)

# Hide any visual output 

# Strip prefix and extract parameters (e.g. dymoprint://SKU123?qty=5)
$raw = $Url -replace "^dymoprint://", "" -replace "/$", ""
$sku = $raw
$qty = 1

if ($raw -match "\?qty=(\d+)") {
    $qty = $Matches[1]
    $sku = $raw -replace "\?qty=\d+", ""
}

# Decode any URL encoded characters (like spaces)
Add-Type -AssemblyName System.Web
$sku = [System.Web.HttpUtility]::UrlDecode($sku)

# Bypass localhost SSL errors natively within PowerShell
[System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 -bor [System.Net.SecurityProtocolType]::Tls11 -bor [System.Net.SecurityProtocolType]::Tls13

$labelXml = @"
<?xml version="1.0" encoding="utf-8"?>
<DesktopLabel Version="1">
  <DYMOLabel Version="3">
    <Description>DYMO Label</Description>
    <Orientation>Portrait</Orientation>
    <LabelName>30332 1 in x 1 in</LabelName>
    <InitialLength>0</InitialLength>
    <BorderStyle>SolidLine</BorderStyle>
    <Margin>
      <Top>0</Top><Left>0</Left><Right>0</Right><Bottom>0</Bottom>
    </Margin>
    <ObjectInfo>
      <BarcodeObject>
        <Name>Barcode</Name>
        <ForeColor Alpha="255" Red="0" Green="0" Blue="0" />
        <BackColor Alpha="0" Red="255" Green="255" Blue="255" />
        <LinkedObjectName />
        <Rotation>Rotation0</Rotation>
        <IsMirrored>False</IsMirrored>
        <IsVariable>True</IsVariable>
        <GroupID>-1</GroupID>
        <IsOutlined>False</IsOutlined>
        <Text>$sku</Text>
        <Type>QRCode</Type>
        <Size>Large</Size>
        <TextPosition>None</TextPosition>
        <TextFont Family="Arial" Size="8" Bold="False" Italic="False" Underline="False" Strikeout="False" />
        <CheckSumFont Family="Arial" Size="8" Bold="False" Italic="False" Underline="False" Strikeout="False" />
        <TextEmbedding>None</TextEmbedding>
        <ECLevel>0</ECLevel>
        <HorizontalAlignment>Center</HorizontalAlignment>
        <QuietZonesPadding Left="0" Top="0" Right="0" Bottom="0" />
      </BarcodeObject>
      <ObjectBounds X="144" Y="144" Width="1152" Height="1152" />
    </ObjectInfo>
  </DYMOLabel>
</DesktopLabel>
"@

$printParamsXml = "<PrintParams><Copies>$qty</Copies></PrintParams>"

# Ping DYMO Connect to find the active Port
$ports = @("https://127.0.0.1:41951", "http://127.0.0.1:41952", "https://localhost:41951", "http://localhost:41952")
$apiUrl = $null

foreach ($port in $ports) {
    try {
        $req = Invoke-WebRequest -Uri "$port/DYMO/DLS/Printing/StatusConnected" -Method Get -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        if ($req.Content.Trim() -match "(?i)true") {
            $apiUrl = "$port/DYMO/DLS/Printing"
            break
        }
    } catch {
        # ignore ping errors and try next
    }
}

Add-Type -AssemblyName System.Windows.Forms

if (-not $apiUrl) {
    [System.Windows.Forms.MessageBox]::Show("DYMO Connect software is not running or unreachable. Please launch DYMO Web Service from your Start Menu.", "RSCP DYMO Bridge Error", 0, 16)
    exit
}

# Fetch the active printer from DYMO Software
try {
    $printersXmlStr = (Invoke-WebRequest -Uri "$apiUrl/GetPrinters" -Method Get).Content
    $xml = [xml]$printersXmlStr
    
    # Try getting the first valid printer Node
    $printerNode = $xml.Printers.SelectSingleNode("//*[(local-name()='LabelWriterPrinter' or local-name()='TapePrinter') and Name]")
    
    if (-not $printerNode) {
        throw "Make sure a printer is installed and visible in DYMO Connect."
    }

    $printerName = $printerNode.Name

    $body = @{
        printerName = $printerName
        printParamsXml = $printParamsXml
        labelXml = $labelXml
        labelSetXml = ""
    }
    
    # Send absolute silent print request to the local DYMO Spooler
    $printRes = Invoke-RestMethod -Uri "$apiUrl/PrintLabel" -Method Post -Body $body
    
} catch {
    [System.Windows.Forms.MessageBox]::Show("DYMO Print Job failed to initialize: $_", "RSCP DYMO Bridge Error", 0, 16)
}

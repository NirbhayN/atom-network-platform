## XSD Changes Summary
Adds purposeCode to the transfer request.

## Updated XSD File(s)

### `transfer_request.xsd`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="urn:example:transfer">
  <xs:element name="TransferRequest">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="Amount" type="xs:decimal">
      </xs:sequence>
    </xs:complexType>
</xs:schema>
```

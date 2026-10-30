## XSD Changes Summary
Adds an optional `purposeCode` element to the transfer request message so an
institution can classify outbound transfers. One schema file changes; the new
element is optional, so existing senders remain valid.

## Diff Annotation Legend
```
<!-- [NEW] -->     — newly added element or type
<!-- [MODIFIED] → previous definition --> — modified element
<!-- [DEPRECATED] --> — element retained for backward compatibility but deprecated
```

## Updated XSD File(s)

### `transfer_request.xsd`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="urn:example:transfer" xmlns="urn:example:transfer"
           elementFormDefault="qualified">
  <xs:simpleType name="PurposeCode"> <!-- [NEW] -->
    <xs:restriction base="xs:string">
      <xs:pattern value="[A-Z]{4}"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:element name="TransferRequest">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="Amount" type="xs:decimal"/>
        <xs:element name="purposeCode" type="PurposeCode" minOccurs="0"/> <!-- [NEW] -->
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
```

## Migration Notes
- Publish the updated `transfer_request.xsd` to the schema registry.
- No namespace version bump: the new element is optional (backward compatible).

## Partner Impact
Senders are unaffected until they opt in; receivers must regenerate JAXB
bindings to read `purposeCode` when present.

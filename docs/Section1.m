section Section1;

shared Table1 = let
    Source = Excel.CurrentWorkbook(){[Name="Table1"]}[Content],
    #"Changed Type" = Table.TransformColumnTypes(Source,{{"Company", type text}, {"PO #", Int64.Type}, {"Adjustment Date", type datetime}, {"Vendor", type text}, {"Team/Performer", type text}, {"Opponent/Performer", type text}, {"Event Date", type datetime}, {"Seat Section", type text}, {"Seat Row", type text}, {"Seats", type text}, {"Ticket Cost Start", type number}, {"Ticket Cost End", type number}, {"Qty Start", Int64.Type}, {"Qty End", Int64.Type}, {"Ticket Cost Total Start", type number}, {"Ticket Cost Total End", type number}, {"Per Ticket Adjustment", type number}, {"Total Adjustment", type number}, {"Cancelled", type text}, {"User", type text}}),
    #"Removed Columns" = Table.RemoveColumns(#"Changed Type",{"Opponent/Performer", "Event Date", "Seat Section", "Seat Row", "Seats", "Ticket Cost Start", "Ticket Cost End", "Qty Start", "Qty End"}),
    #"Renamed Columns" = Table.RenameColumns(#"Removed Columns",{{"Ticket Cost Total Start", "Total Start"}, {"Ticket Cost Total End", "Total End"}}),
    #"Changed Type1" = Table.TransformColumnTypes(#"Renamed Columns",{{"Total Start", Currency.Type}, {"Total End", Currency.Type}, {"Per Ticket Adjustment", Currency.Type}, {"Total Adjustment", Currency.Type}}),
    #"Removed Columns1" = Table.RemoveColumns(#"Changed Type1",{"Per Ticket Adjustment"}),
    #"Replaced Value" = Table.ReplaceValue(#"Removed Columns1","YSA 2","YSA",Replacer.ReplaceText,{"Company"}),
    #"Replaced Value1" = Table.ReplaceValue(#"Replaced Value","YSA 3","YSA",Replacer.ReplaceText,{"Company"}),
    #"Replaced Value2" = Table.ReplaceValue(#"Replaced Value1","Bearhawk - Aaron","Bearhawk Group",Replacer.ReplaceText,{"Company"}),
    #"Replaced Value3" = Table.ReplaceValue(#"Replaced Value2","Bearhawk - Chris","Bearhawk Group",Replacer.ReplaceText,{"Company"}),
    #"Replaced Value4" = Table.ReplaceValue(#"Replaced Value3","Bearhawk - Dylan","Bearhawk Group",Replacer.ReplaceText,{"Company"}),
    #"Grouped Rows" = Table.Group(#"Replaced Value4", {"Company", "PO #", "Adjustment Date", "Vendor", "Team/Performer", "Cancelled", "User"}, {{"Total Start", each List.Sum([Total Start]), type nullable number}, {"Total End", each List.Sum([Total End]), type nullable number}, {"Total Adjustment", each List.Sum([Total Adjustment]), type nullable number}}),
    #"Added Conditional Column" = Table.AddColumn(#"Grouped Rows", "Total Adjustment 2", each if [Cancelled] = "Yes" then -[Total End] else [Total Adjustment]),
    #"Removed Columns2" = Table.RemoveColumns(#"Added Conditional Column",{"Total Adjustment"}),
    #"Renamed Columns1" = Table.RenameColumns(#"Removed Columns2",{{"Total Adjustment 2", "Total Adjustment"}}),
    #"Sorted Rows" = Table.Sort(#"Renamed Columns1",{{"PO #", Order.Ascending}}),
    #"Reordered Columns" = Table.ReorderColumns(#"Sorted Rows",{"Company", "PO #", "Adjustment Date", "Vendor", "Team/Performer", "Total Start", "Total End", "Total Adjustment", "Cancelled", "User"}),
    #"Changed Type2" = Table.TransformColumnTypes(#"Reordered Columns",{{"Total Start", Currency.Type}, {"Total End", Currency.Type}, {"Total Adjustment", Currency.Type}}),
    #"Filtered Rows" = Table.SelectRows(#"Changed Type2", each [Total Adjustment] <> 0),
    #"Added Conditional Column1" = Table.AddColumn(#"Filtered Rows", "Cancelled2", each if [Total End] = 0 then "Yes" else if [Cancelled] = "Yes" then "Yes" else " "),
    #"Removed Columns3" = Table.RemoveColumns(#"Added Conditional Column1",{"Cancelled"}),
    #"Reordered Columns1" = Table.ReorderColumns(#"Removed Columns3",{"Company", "PO #", "Adjustment Date", "Vendor", "Team/Performer", "Total Start", "Total End", "Total Adjustment", "Cancelled2", "User"}),
    #"Renamed Columns2" = Table.RenameColumns(#"Reordered Columns1",{{"Cancelled2", "Cancelled"}})
in
    #"Renamed Columns2";
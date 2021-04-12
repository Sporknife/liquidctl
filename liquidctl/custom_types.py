from typing import List, Dict, Tuple, Any, Union, Final

SupportedDevicesType = List[Tuple[
    int, int, None, str,
    Dict[
        str,
        Union[
            str, int, 
            Tuple[float], 
            Dict[str, Tuple[int, ...]],
            Dict[str, int],
        ]
    ]
]]
_ColorModesType = Dict[str, Tuple[Union[int, bool, None], ...]]

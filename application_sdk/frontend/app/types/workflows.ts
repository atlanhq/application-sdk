export interface RadioWidget {
    widget: "radio";
    label?: string;
    hidden?: boolean;
    help?: string;
    placeholder?: string;
    rules?: Array<{ required?: boolean; message?: string }>;
    grid?: number;
    enumNames?: string[];
    enum?: string[];
}

export interface CredentialWidget {
    widget: "credential";
    label?: string;
    credentialType?: string;
    placeholder?: string;
    hidden?: boolean;
}

export interface InputWidget {
    label?: string;
    placeholder?: string;
    hidden?: boolean;
    feedback?: boolean;
    help?: string;
    grid?: number;
    start?: number;
    default?: string | number | boolean;
    rules?: Array<{ required?: boolean; message?: string }>;
    message?: string;
    BYOCdisabled?: boolean;
}

export interface PasswordWidget {
    widget: "password";
    label?: string;
    placeholder?: string;
    hidden?: boolean;
    feedback?: boolean;
    help?: string;
    grid?: number;
    default?: string | number | boolean;
    rules?: Array<{ required?: boolean; message?: string }>;
    message?: string;
}

export interface NestedWidget {
    widget: "nested";
    label?: string;
    header?: string;
    placeholder?: string;
    nestedValue?: boolean;
    hidden?: boolean;
}

export interface SelectWidget {
    widget: "select";
    label?: string;
    placeholder?: string;
    grid?: number;
    hidden?: boolean;
}